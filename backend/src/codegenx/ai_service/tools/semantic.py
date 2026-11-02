"""
语义层检索工具：让数据分析智能体按需查“数据地图”，而不是把全量 schema 塞进 prompt。

工具分工：
  - search_tables       粗召回：问题 -> 候选表卡
  - describe_table_card 精读：单表列卡/主键/注释
  - find_join_path      关系：两表之间的 JOIN 路径（带置信度）
  - search_metrics      指标口径
  - find_verified_queries 历史已验证问答（few-shot）

参数约定：datasource 省略时取当前会话 db_name；语义层元数据来自 PostgreSQL semantic schema。
"""
from __future__ import annotations

import asyncio
from typing import Any

from codegenx.ai_service.semantic.model.cards import (
    fmt_join_path,
    fmt_metric_card,
    fmt_table_card,
    fmt_table_detail,
    fmt_verified_query,
)
from codegenx.ai_service.semantic.retrieval import hybrid
from codegenx.ai_service.semantic.store import pg_store
from codegenx.ai_service.tools.base import BaseTool, ToolResult
from codegenx.ai_service.utils.config import config
from shared import log


class SemanticTool(BaseTool):
    """语义层检索工具基类：观测类别 retrieval（trace 区分召回质量与工具执行）。"""

    @property
    def obs_category(self) -> str:
        return "retrieval"


def _datasource(params: dict, session_state: Any) -> str | None:
    """数据源优先级：显式参数 > 会话当前数据集 > 可用白名单首个 > store 内部兜底。

    current_db / available_schemas 元素格式均为 "datasource.schema"
    （如 business.dvdrental），取其 datasource 部分作为语义层数据源名。
    """
    explicit = (params.get("datasource") or "").strip()
    if explicit:
        return explicit.split(".")[0]
    current = getattr(session_state, "current_db", None)
    if current:
        return current.split(".")[0]
    available = list(getattr(session_state, "available_schemas", None) or [])
    if available:
        return available[0].split(".")[0]
    return getattr(session_state, "db_name", None)


def _clamp(value: Any, default: int, upper: int = 20) -> int:
    try:
        return max(1, min(int(value), upper))
    except (TypeError, ValueError):
        return default


def _split_table_ref(raw: str) -> tuple[str | None, str | None, str]:
    """解析表引用：支持 table / schema.table / datasource.schema.table。"""
    parts = [p for p in (raw or "").split(".") if p]
    if len(parts) >= 3:
        return parts[-3], parts[-2], parts[-1]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, (parts[0] if parts else "")


async def _failure(exc: Exception) -> ToolResult:
    """把 LookupError 转成可自愈的提示（附带已注册数据源）。"""
    message = str(exc)
    if isinstance(exc, LookupError):
        try:
            known = [d["name"] for d in await pg_store.list_datasources()]
        except Exception:
            known = []
        message = f"{message}；已注册数据源: {', '.join(known) or '无'}"
    return ToolResult(success=False, message=message)


class SearchTablesTool(SemanticTool):

    @property
    def name(self) -> str:
        return "search_tables"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "在语义层检索候选表卡（混合向量+关键词）。用于把自然语言问题映射到表，"
            "返回表名、业务含义、粒度、主键、行数、标签和匹配分。"
            "不确定该用哪些表时先调用它；拿到表名后再用 describe_table_card 看列。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言问题或关键词，如“每个门店的月度销售额”"},
                "datasource": {"type": "string", "description": "数据源名称，省略则用当前会话库"},
                "domain": {"type": "string", "description": "主题域过滤，如 销售/会员/库存，可省略"},
                "top_k": {"type": "integer", "description": "返回条数，默认 8，最大 20"},
            },
            "required": ["query"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None, session_state: Any = None,
    ) -> ToolResult:
        try:
            rows = await hybrid.search_tables(
                params["query"],
                datasource=_datasource(params, session_state),
                domain=(params.get("domain") or "").strip() or None,
                top_k=_clamp(params.get("top_k"), config.semantic.recall_top_k),
            )
        except Exception as exc:
            log.warning("search_tables 失败: {}", exc)
            return await _failure(exc)

        if not rows:
            return ToolResult(
                success=True,
                data="（未召回相关表。可换更贴近业务的同义词重试，或先确认语义层已采集）",
                render="未召回相关表",
            )
        text = "\n".join(fmt_table_card(r, r.get("score")) for r in rows)
        # extras.tables：结构化表名（trace recalled_tables → 评测归因"SQL 错因召回缺表"）
        return ToolResult(
            success=True, data=text, render=f"召回 {len(rows)} 张表",
            extras={"tables": [
                f"{r.get('datasource', '')}.{r.get('schema_name', '')}.{r.get('name', '')}"
                for r in rows
            ]},
        )


class DescribeTableCardTool(SemanticTool):

    @property
    def name(self) -> str:
        return "describe_table_card"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "读取语义层中的单表卡片：列清单、类型、主键、列注释、同义词、敏感字段标记。"
            "不访问业务库实时结构。表名不唯一时返回候选，需补充 schema 参数。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名，可写 schema.table"},
                "schema": {"type": "string", "description": "schema 名，可省略"},
                "datasource": {"type": "string", "description": "数据源名称，省略则用当前会话库"},
                "detail": {
                    "type": "string", "enum": ["summary", "full"],
                    "description": "summary=仅表卡；full=含列清单（默认 full）",
                },
            },
            "required": ["table"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None, session_state: Any = None,
    ) -> ToolResult:
        table = (params.get("table") or "").strip()
        schema = (params.get("schema") or "").strip() or None
        ds_from_ref, schema_from_ref, table = _split_table_ref(table)
        schema = schema or schema_from_ref
        datasource = _datasource(params, session_state) or ds_from_ref

        try:
            cards = await pg_store.get_table_cards(datasource, table, schema)
            if not cards:
                hint = f"{schema + '.' if schema else ''}{table}"
                return ToolResult(success=False, message=f"语义层未登记表 {hint}，请先用 search_tables 检索")
            if len(cards) > 1:
                names = ", ".join(f"{c['schema_name']}.{c['name']}" for c in cards)
                return ToolResult(
                    success=False,
                    message=f"表名不唯一，请补充 schema 参数。候选：{names}",
                )

            card = cards[0]
            if params.get("detail", "full") == "summary":
                text = fmt_table_card(card)
            else:
                columns = await pg_store.get_columns(card["id"])
                text = fmt_table_detail(card, columns)
        except Exception as exc:
            log.warning("describe_table_card 失败: {}", exc)
            return await _failure(exc)

        return ToolResult(success=True, data=text, render=f"{card['schema_name']}.{card['name']}")


class FindJoinPathTool(SemanticTool):

    @property
    def name(self) -> str:
        return "find_join_path"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "查询两张表之间已登记的 JOIN 路径（外键/语义模型声明/推断边），返回连接列和置信度。"
            "生成多表 SQL 前必须用它确认连接键，不要自行猜测 JOIN 条件。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from_table": {"type": "string", "description": "起始表，可写 schema.table"},
                "to_table": {"type": "string", "description": "目标表，可写 schema.table"},
                "datasource": {"type": "string", "description": "数据源名称，省略则用当前会话库"},
                "max_hops": {"type": "integer", "description": "最大跳数，默认 3，最大 5"},
            },
            "required": ["from_table", "to_table"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None, session_state: Any = None,
    ) -> ToolResult:
        max_hops = min(_clamp(params.get("max_hops"), config.semantic.max_join_hops, upper=5), 5)
        from_ds, from_schema, from_name = _split_table_ref(params["from_table"])
        to_ds, to_schema, to_name = _split_table_ref(params["to_table"])
        datasource = _datasource(params, session_state) or from_ds or to_ds
        from_ref = f"{from_schema}.{from_name}" if from_schema else from_name
        to_ref = f"{to_schema}.{to_name}" if to_schema else to_name
        try:
            paths = await pg_store.find_join_path(
                datasource, from_ref, to_ref, max_hops=max_hops,
            )
        except Exception as exc:
            log.warning("find_join_path 失败: {}", exc)
            return await _failure(exc)

        return ToolResult(
            success=True,
            data=fmt_join_path(paths),
            render=f"JOIN 路径 {len(paths)} 条",
        )


class SearchMetricsTool(SemanticTool):

    @property
    def name(self) -> str:
        return "search_metrics"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "检索业务指标卡（名称/同义词/口径/表达式/可下钻维度）。"
            "当问题涉及销售额、转化率、客单价等业务口径时先调用它，按指标口径写 SQL。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "指标名或业务问题，如“客单价怎么算”"},
                "datasource": {"type": "string", "description": "数据源名称，省略则用当前会话库"},
                "top_k": {"type": "integer", "description": "返回条数，默认 5，最大 20"},
            },
            "required": ["query"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None, session_state: Any = None,
    ) -> ToolResult:
        try:
            rows = await hybrid.search_metrics(
                params["query"],
                datasource=_datasource(params, session_state),
                top_k=_clamp(params.get("top_k"), config.semantic.metric_top_k),
            )
        except Exception as exc:
            log.warning("search_metrics 失败: {}", exc)
            return await _failure(exc)

        if not rows:
            return ToolResult(success=True, data="（未登记相关指标口径）", render="未召回指标")
        text = "\n".join(fmt_metric_card(r, r.get("score")) for r in rows)
        return ToolResult(success=True, data=text, render=f"召回 {len(rows)} 个指标")


class FindVerifiedQueriesTool(SemanticTool):

    @property
    def name(self) -> str:
        return "find_verified_queries"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "召回与当前问题相似的历史已验证问答（问题 + SQL）。"
            "用作写法参考（表/过滤/口径），不要照搬与当前问题无关的部分。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "当前自然语言问题"},
                "datasource": {"type": "string", "description": "数据源名称，省略则用当前会话库"},
                "top_k": {"type": "integer", "description": "返回条数，默认 3，最大 10"},
            },
            "required": ["question"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None, session_state: Any = None,
    ) -> ToolResult:
        try:
            rows = await hybrid.search_verified_queries(
                params["question"],
                datasource=_datasource(params, session_state),
                top_k=_clamp(params.get("top_k"), config.semantic.verified_query_top_k, upper=10),
            )
        except Exception as exc:
            log.warning("find_verified_queries 失败: {}", exc)
            return await _failure(exc)

        if not rows:
            return ToolResult(success=True, data="（暂无相似的历史已验证问答）", render="无示例")
        text = "\n\n".join(fmt_verified_query(r, r.get("score")) for r in rows)
        return ToolResult(success=True, data=text, render=f"召回 {len(rows)} 条示例")
