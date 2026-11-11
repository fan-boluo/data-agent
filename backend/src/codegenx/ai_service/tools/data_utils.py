"""
通用数据分析辅助工具

- guess_analysis_task: 根据数据结构推断分析方向
- get_table_relationships: 表间关系发现(适用于mysql数据库,已不再注册使用)
"""

import asyncio
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log


# ═══════════════════════════════════════════════════════════
# 别名常量——避免硬依赖 data_mysql 模块
# ═══════════════════════════════════════════════════════════

def _get_mysql_manager():
    """延迟导入，避免循环依赖；F-4 起共享 mysql_manager 进程级单例（配置来自 config.json）。"""
    from codegenx.ai_service.agent.db.mysql_manager import get_mysql_manager
    return get_mysql_manager()


# 列类型优先级 SQL（复用 data_mysql 中的逻辑）
_COL_TYPE_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY ORDINAL_POSITION"
)

_TABLE_LIST_SQL = (
    "SELECT TABLE_NAME FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'"
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
            "至少需要提供 (db_name + table_name) 或 file_path 之一。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "db_name": {
                    "type": "string",
                    "description": "数据库名称（MySQL 数据源）",
                },
                "table_name": {
                    "type": "string",
                    "description": "表名",
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
        db_name = params.get("db_name") or (await ToolSession.load(params)).db_name or None
        table_name = params.get("table_name")
        file_path = params.get("file_path")

        if not db_name and not file_path:
            return ToolResult(
                success=False,
                message="需要提供 db_name 或 file_path",
            )

        n_tables = 1
        columns: list[tuple[str, str]] = []
        n_rows = 0

        if db_name and table_name:
            mgr = _get_mysql_manager()
            try:
                col_rows = await mgr.query(db_name, _COL_TYPE_SQL, (db_name, table_name))
                columns = [(r["COLUMN_NAME"], r["DATA_TYPE"]) for r in col_rows]
                n_rows = await mgr.get_table_rows_estimate(db_name, table_name)
                # 表数
                table_rows = await mgr.query(db_name, _TABLE_LIST_SQL, (db_name,))
                n_tables = len(table_rows)
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


# ═══════════════════════════════════════════════════════════
# 9. get_table_relationships
# ═══════════════════════════════════════════════════════════

# 查询显式外键
_FK_SQL = (
    "SELECT "
    "  COLUMN_NAME AS fk_column, "
    "  REFERENCED_TABLE_NAME AS ref_table, "
    "  REFERENCED_COLUMN_NAME AS ref_column, "
    "  CONSTRAINT_NAME AS constraint_name "
    "FROM information_schema.KEY_COLUMN_USAGE "
    "WHERE TABLE_SCHEMA = %s "
    "  AND REFERENCED_TABLE_SCHEMA = %s "
    "  AND REFERENCED_TABLE_NAME IS NOT NULL"
)


def _infer_relationships(
    all_tables: dict[str, list[str]],
    min_similarity: float = 0.7,
) -> list[dict]:
    """根据列名相似度推断可能的关联关系。"""
    relations = []
    table_names = list(all_tables.keys())

    # 生成所有列到表的反向索引（只取含 _id 或 id 的列）
    col_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for tbl, cols in all_tables.items():
        for col in cols:
            col_lower = col.lower()
            if col_lower.endswith("_id") or col_lower == "id":
                col_index[col_lower].append((tbl, col))

    for tbl_a, cols_a in all_tables.items():
        for col_a in cols_a:
            col_a_lower = col_a.lower()
            if not (col_a_lower.endswith("_id") or col_a_lower == "id"):
                continue

            # 找匹配的 "id" 列
            # user_id → id in another table
            for tbl_b, cols_b in all_tables.items():
                if tbl_b == tbl_a:
                    continue
                for col_b in cols_b:
                    col_b_lower = col_b.lower()
                    # 匹配规则: a.user_id → b.id
                    if col_a_lower == "id":
                        # 检查 b 中是否有 xxx_id 指回 a
                        ref_col = f"{tbl_a.lower()}_id"
                        if col_b_lower == ref_col:
                            relations.append({
                                "from_table": tbl_b,
                                "from_column": col_b,
                                "to_table": tbl_a,
                                "to_column": col_a,
                                "type": "inferred",
                                "confidence": "medium",
                            })
                    elif col_b_lower == "id":
                        # a.xxx_id → b.id
                        # 检查列名去掉 _id 后缀后是否匹配另一个表名
                        prefix = col_a_lower[:-3]  # 去掉 _id
                        if prefix == tbl_b.lower() or _similar(prefix, tbl_b.lower()) > min_similarity:
                            relations.append({
                                "from_table": tbl_a,
                                "from_column": col_a,
                                "to_table": tbl_b,
                                "to_column": col_b,
                                "type": "inferred",
                                "confidence": "high" if prefix == tbl_b.lower() else "medium",
                            })

    return relations


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class GetTableRelationshipsTool:

    @property
    def name(self) -> str:
        return "get_table_relationships"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回数据库中表之间的外键关系（显式）和基于列名推断的潜在关联关系。"
            "用于辅助编写多表 JOIN 的 SQL。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "db_name": {
                    "type": "string",
                    "description": "数据库名称",
                },
            },
            "required": ["db_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("get_table_relationships 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取表关系失败: {exc}")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        db_name = params["db_name"]
        mgr = _get_mysql_manager()

        # 显式外键
        fk_rows = await mgr.query(db_name, _FK_SQL, (db_name, db_name))

        # 获取所有表和它们的列
        table_rows = await mgr.query(db_name, _TABLE_LIST_SQL, (db_name,))
        all_tables: dict[str, list[str]] = {}
        for tr in table_rows:
            tname = tr["TABLE_NAME"]
            try:
                cols = await mgr.query(
                    db_name,
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    (db_name, tname),
                )
                all_tables[tname] = [c["COLUMN_NAME"] for c in cols]
            except Exception:
                all_tables[tname] = []

        # 推断关系
        inferred = _infer_relationships(all_tables)

        lines = [f"数据库 {db_name} 表关系:"]

        if fk_rows:
            lines.append("")
            lines.append("── 显式外键 ──")
            for fk in fk_rows:
                lines.append(
                    f"  {fk['fk_column']} → {fk['ref_table']}.{fk['ref_column']} "
                    f"({fk['constraint_name']})"
                )

        if inferred:
            lines.append("")
            lines.append("── 推断关系（基于列名）──")
            seen = set()
            for rel in inferred:
                key = (rel["from_table"], rel["to_table"])
                if key not in seen:
                    seen.add(key)
                    lines.append(
                        f"  {rel['from_table']}.{rel['from_column']} "
                        f"→ {rel['to_table']}.{rel['to_column']} "
                        f"[{rel['confidence']} confidence]"
                    )

        if not fk_rows and not inferred:
            lines.append("")
            lines.append("未发现显式外键或可推断的关联关系")

        return ToolResult(success=True, data="\n".join(lines), render=f"表关系查找: {db_name}")
