"""
通用数据分析辅助工具

- guess_analysis_task: 根据数据结构推断分析方向（当前数据集 = PostgreSQL 公共库 / CSV）
"""

import asyncio
import re
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log

# 标识符安全校验：拼进 information_schema 信息查询前的兜底
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# 列类型查询（PostgreSQL；schema 为空时排除系统 schema）
_COLUMNS_SQL = (
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_name = '{table}' {schema_filter} "
    "ORDER BY ordinal_position"
)

# schema 下表数量（多表提示用）
_TABLE_COUNT_SQL = (
    "SELECT count(*) AS n FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace ns ON ns.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND ns.nspname = '{schema}'"
)


# ═══════════════════════════════════════════════════════════
# 8. guess_analysis_task
# ═══════════════════════════════════════════════════════════

def _infer_task_suggestions(
    columns: list[tuple[str, str]],
    n_rows: int = 0,
    n_tables: int = 1,
) -> list[str]:
    """根据列名和类型推断分析方向。columns: [(name, data_type), ...]"""
    suggestions = []
    col_names_lower = [c[0].lower() for c in columns]

    has_time = any(_is_time_type(dt) for _, dt in columns)
    has_numeric = any(_is_numeric_type(dt) for _, dt in columns)
    has_category = any(_is_category_type(dt) for _, dt in columns)
    has_text = any(_is_text_type(dt) for _, dt in columns)

    n_numeric = sum(1 for _, dt in columns if _is_numeric_type(dt))
    n_category = sum(1 for _, dt in columns if _is_category_type(dt))

    # 时间 + 数值 → 时序分析
    if has_time and has_numeric:
        suggestions.append("时序趋势分析 — 按时间维度聚合指标，观察变化趋势和周期性")
        suggestions.append("同比环比分析 — 按日/周/月对比，发现增长点和异常波动")

    # 类别 + 数值 → 分组对比
    if has_category and has_numeric:
        suggestions.append("分组对比分析 — 按类别维度比较各分组均值、排名、差异")

    # 多数值列 → 相关性分析
    if n_numeric >= 3:
        suggestions.append("相关性分析 — 分析数值指标之间的关联关系，发现驱动因素")

    # 多表 → 关联分析
    if n_tables > 1:
        suggestions.append("多表关联分析 — 通过 JOIN 整合多表数据，进行跨维度分析")

    # 文本 → 文本分析
    if has_text:
        suggestions.append("文本分析 — 对文本字段做词频统计、关键词提取、情感分析")

    # 只有类别 → 分布分析
    if n_category >= 2 and n_numeric == 0:
        suggestions.append("分布与构成分析 — 分析类别变量的取值分布和交叉关系")

    if not suggestions:
        suggestions.append("描述性统计 — 先了解数据的基本分布特征")

    return suggestions[:5]


def _is_numeric_type(dtype: str) -> bool:
    dt = dtype.lower()
    return any(t in dt for t in ("int", "float", "double", "decimal", "numeric"))


def _is_time_type(dtype: str) -> bool:
    dt = dtype.lower()
    return any(t in dt for t in ("datetime", "timestamp", "date", "time"))


def _is_category_type(dtype: str) -> bool:
    dt = dtype.lower()
    return any(t in dt for t in ("char", "varchar", "enum", "set"))


def _is_text_type(dtype: str) -> bool:
    dt = dtype.lower()
    return any(t in dt for t in ("text", "mediumtext", "longtext", "blob"))


class GuessAnalysisTaskTool(BaseTool):

    @property
    def name(self) -> str:
        return "guess_analysis_task"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "根据数据表/CSV 的结构自动推断可能的分析方向（3-5 条建议）。"
            "在用户不太清楚要从什么角度分析时使用，提供分析思路启发。"
            "数据库表只写 table_name（可写 schema.table，dataset 取当前会话），或给 file_path 指向 CSV。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "表名，可写 schema.table；不填 schema 用当前数据集默认 schema",
                },
                "file_path": {
                    "type": "string",
                    "description": "CSV 文件路径",
                },
            },
            "required": [],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("guess_analysis_task 执行异常: {}", exc)
            return ToolResult(success=False, message=f"推断分析任务失败: {exc}")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        table_ref = str(params.get("table_name") or "").strip()
        file_path = params.get("file_path")

        if not table_ref and not file_path:
            return ToolResult(
                success=False,
                message="需要提供 table_name 或 file_path",
            )

        n_tables = 1
        columns: list[tuple[str, str]] = []
        n_rows = 0

        if table_ref:
            # 表引用解析：schema.table / table（schema 缺省用当前数据集）
            parts = [p for p in table_ref.split(".") if p]
            if len(parts) == 2:
                schema, table = parts
            elif len(parts) == 1:
                schema, table = "", parts[0]
            else:
                return ToolResult(success=False, message=f"表名不合法: {table_ref}")
            if not _IDENT_RE.match(table) or (schema and not _IDENT_RE.match(schema)):
                return ToolResult(success=False, message=f"表名或 schema 含非法字符: {table_ref}")

            sess = await ToolSession.load(params)
            db_name = str(params.get("db_name") or sess.db_name).strip()
            if not db_name:
                return ToolResult(
                    success=False,
                    message="未选择数据集，请先让用户选择要分析的数据集",
                )
            schema = schema or str(params.get("db_schema") or sess.db_schema).strip()
            if schema and not _IDENT_RE.match(schema):
                return ToolResult(success=False, message=f"schema 含非法字符: {schema}")

            from codegenx.ai_service.agent.db import postgres_manager
            # 只查信息架构（列类型 + 行数估算 + 表数），走只读连接池
            schema_filter = (
                f"AND table_schema = '{schema}'" if schema
                else "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
            try:
                col_rows = await postgres_manager.query(
                    db_name, _COLUMNS_SQL.format(table=table, schema_filter=schema_filter), schema=schema,
                )
                columns = [(r["column_name"], r["data_type"]) for r in col_rows]
                n_rows = await postgres_manager.get_table_rows_estimate(db_name, table, schema=schema)
                if schema:
                    cnt_rows = await postgres_manager.query(
                        db_name, _TABLE_COUNT_SQL.format(schema=schema), schema=schema,
                    )
                    n_tables = int(cnt_rows[0]["n"]) if cnt_rows else 1
            except Exception as exc:
                return ToolResult(success=False, message=f"查询表信息失败: {exc}")

        elif file_path:
            from codegenx.ai_service.data_analysis.csv_manager import CsvManager
            _csv_mgr = CsvManager()
            info = _csv_mgr.read_info(file_path)
            columns = [(c, "unknown") for c in info["columns"]]
            n_rows = info["estimated_rows"]

        if not columns:
            return ToolResult(success=False, message="未找到任何列信息")

        suggestions = _infer_task_suggestions(columns, n_rows, n_tables)

        col_summary = ", ".join(
            f"{name}({dtype})" for name, dtype in columns[:20]
        )
        if len(columns) > 20:
            col_summary += f"... 共 {len(columns)} 列"

        lines = [
            f"数据概况: {len(columns)} 列, {n_rows} 行",
            f"列: {col_summary}",
            "",
            "── 推荐分析方向 ──",
        ]
        for i, s in enumerate(suggestions, 1):
            lines.append(f"{i}. {s}")

        return ToolResult(success=True, data="\n".join(lines), render=f"分析建议: {len(columns)} 列, {n_rows} 行")
