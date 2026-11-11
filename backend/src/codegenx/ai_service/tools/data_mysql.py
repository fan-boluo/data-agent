"""
MySQL 数据分析工具（已废弃：问答链路已全面切换到 PostgreSQL 公共库，见 data_postgres.py）。

以下类不再继承 BaseTool，故不会被工具注册表收录；代码仅作历史参考保留。
PG 时代的表结构/关系探查由 semantic.py 的语义层工具承担（describe_table_card / find_join_path），
采样与统计直接通过 execute_postgresql 写 SQL 完成。

- list_tables: 列出库下所有表
- describe_table: 单表结构信息
- sample_rows: 表数据采样
- describe_table_stats: 详细统计信息
"""

import asyncio
from typing import Any
from codegenx.ai_service.utils.dbutils import format_rows
from codegenx.ai_service.tools.base import ToolResult
from codegenx.ai_service.agent.db.mysql_manager import (
    get_mysql_manager, resolve_sample_ratio,
)
from codegenx.ai_service.data_analysis.stats_engine import (
    compute_numeric_stats, compute_categorical_stats,
    format_numeric_stats, format_categorical_stats, format_correlation,
    _resolve_sample_pct,
)
from shared import log

import numpy as np

# 宽表列数上限
MAX_COLS_WIDE_TABLE = 20
# 列统计超时（秒）
COL_STATS_TIMEOUT = 30

# ── 单例 ──────────────────────────────────────────────────
# 复用 mysql_manager.get_mysql_manager() 共享进程级单例（连接参数来自 .env）


# ═══════════════════════════════════════════════════════════
# 1. list_tables
# ═══════════════════════════════════════════════════════════

_LIST_TABLES_SQL = (
    "SELECT TABLE_NAME AS table_name, TABLE_COMMENT AS comment "
    "FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
    "ORDER BY TABLE_NAME"
)


class ListTablesTool:

    @property
    def name(self) -> str:
        return "list_tables"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "列出指定数据库中所有表的信息，包括表名和注释。"
            "用于了解数据库中有哪些表可用。"
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
            log.warning("list_tables 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取表列表失败: {exc}", render=f"数据库查询失败")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        db_name = params["db_name"]
        mgr = get_mysql_manager()
        rows = await mgr.query(db_name, _LIST_TABLES_SQL, (db_name,))

        if not rows:
            return ToolResult(success=True, data=f"数据库 {db_name} 中没有表", render=f"数据库 {db_name}: 无表")

        # 补充行数估算
        for r in rows:
            try:
                est = await mgr.get_table_rows_estimate(db_name, r["table_name"])
                r["estimated_rows"] = est
            except Exception:
                r["estimated_rows"] = "未知"

        output = f"数据库 {db_name} 共有 {len(rows)} 张表:\n"
        output += format_rows(rows)
        return ToolResult(success=True, data=output, render=f"数据库 {db_name}: {len(rows)} 张表")


# ═══════════════════════════════════════════════════════════
# 2. describe_table
# ═══════════════════════════════════════════════════════════

_COLUMNS_SQL = (
    "SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, "
    "IS_NULLABLE AS nullable, COLUMN_DEFAULT AS default_value, "
    "COLUMN_COMMENT AS comment "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY ORDINAL_POSITION"
)

_TABLE_COMMENT_SQL = (
    "SELECT TABLE_COMMENT FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)

_INDEX_SQL = (
    "SELECT INDEX_NAME AS index_name, "
    "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns, "
    "NON_UNIQUE, INDEX_TYPE AS type "
    "FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "GROUP BY INDEX_NAME, NON_UNIQUE, INDEX_TYPE"
)


class DescribeTableTool:

    @property
    def name(self) -> str:
        return "describe_table"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回指定表的详细结构信息：列名、数据类型、是否可空、默认值、注释、"
            "主键和索引信息、行数估算。用于理解表结构后再编写 SQL。"
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
                "table_name": {
                    "type": "string",
                    "description": "表名",
                },
            },
            "required": ["db_name", "table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_table 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取表结构失败: {exc}", render=f"表结构查询失败")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        db_name = params["db_name"]
        table_name = params["table_name"]
        mgr = get_mysql_manager()

        # 并行查询
        cols_task = mgr.query(db_name, _COLUMNS_SQL, (db_name, table_name))
        comment_task = mgr.query(db_name, _TABLE_COMMENT_SQL, (db_name, table_name))
        index_task = mgr.query(db_name, _INDEX_SQL, (db_name, table_name))
        est_rows_task = mgr.get_table_rows_estimate(db_name, table_name)

        columns, comment_rows, indexes, est_rows = await asyncio.gather(
            cols_task, comment_task, index_task, est_rows_task,
        )

        comment = comment_rows[0].get("TABLE_COMMENT", "") if comment_rows else ""
        n_cols = len(columns)
        primary_keys = [idx for idx in indexes if idx.get("NON_UNIQUE") == 0]

        lines = [
            f"表: {db_name}.{table_name}",
        ]
        if comment:
            lines.append(f"注释: {comment}")
        lines.append(f"列数: {n_cols} | 估算行数: {est_rows or '未知'}")
        lines.append("")

        # 列信息
        lines.append("── 列信息 ──")
        lines.append(format_rows(columns))

        # 索引
        if indexes:
            lines.append("")
            lines.append("── 索引 ──")
            lines.append(format_rows(indexes))

        return ToolResult(success=True, data="\n".join(lines), render=f"表结构: {db_name}.{table_name} ({n_cols} 列)")


# ═══════════════════════════════════════════════════════════
# 3. sample_rows
# ═══════════════════════════════════════════════════════════

class SampleRowsTool:

    @property
    def name(self) -> str:
        return "sample_rows"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回表的采样数据（默认 10 行），用于理解字段的实际值和内容格式。"
            "长文本会被截断到 200 字符。"
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
                "table_name": {
                    "type": "string",
                    "description": "表名",
                },
                "limit": {
                    "type": "integer",
                    "description": "采样行数，默认 10",
                },
            },
            "required": ["db_name", "table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sample_rows 执行异常: {}", exc)
            return ToolResult(success=False, message=f"采样数据失败: {exc}", render=f"{self.name} 采样失败")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        db_name = params["db_name"]
        table_name = params["table_name"]
        limit = int(params.get("limit", 10))
        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100

        mgr = get_mysql_manager()
        # 先查列名
        cols = await mgr.query(
            db_name,
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (db_name, table_name),
        )
        col_names = [c["COLUMN_NAME"] for c in cols]
        if not col_names:
            return ToolResult(success=False, message=f"表 {db_name}.{table_name} 不存在或无列", render=f"表不存在: {table_name}")

        # 查数据
        cols_quoted = ", ".join(f"`{c}`" for c in col_names)
        sql = f"SELECT {cols_quoted} FROM `{db_name}`.`{table_name}` LIMIT {limit}"
        rows = await mgr.query(db_name, sql)

        output = f"表 {db_name}.{table_name} 采样 {len(rows)} 行:\n"
        output += format_rows(rows)
        return ToolResult(success=True, data=output, render=f"采样: {db_name}.{table_name} ({len(rows)} 行)")


# ═══════════════════════════════════════════════════════════
# 4. describe_table_stats
# ═══════════════════════════════════════════════════════════

# 列优先级：数值 > 时间 > 日期 > 低基数字符串
_COL_TYPE_PRIORITY_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY ORDINAL_POSITION"
)


def _rank_column_priority(data_type: str) -> int:
    """返回列选取优先级（越小越优先）。"""
    dt = data_type.lower()
    if any(t in dt for t in ("int", "float", "double", "decimal", "numeric")):
        return 1  # 数值列最高优先
    if any(t in dt for t in ("datetime", "timestamp", "date", "time")):
        return 2  # 时间列
    if any(t in dt for t in ("char", "varchar", "text", "enum")):
        return 3  # 字符串
    return 4


class DescribeTableStatsTool:

    @property
    def name(self) -> str:
        return "describe_table_stats"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回表的详细统计信息：数值字段的均值/方差/分位数，"
            "类别字段的频率分布，缺失值情况，列间相关性提示。"
            "大表会自动降级采样并标注。可通过 columns 指定要统计的列。"
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
                "table_name": {
                    "type": "string",
                    "description": "表名",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要统计的列名列表，不指定则自动选择（最多 20 列）",
                },
                "sample_pct": {
                    "type": "integer",
                    "description": "采样百分比 1-100，默认 100（全量）。大表自动降级",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "单列统计超时秒数，默认 30",
                },
            },
            "required": ["db_name", "table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_table_stats 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取统计信息失败: {exc}", render=f"统计失败")

    async def _do_execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        db_name = params["db_name"]
        table_name = params["table_name"]
        requested_columns = params.get("columns")
        requested_sample_pct = int(params.get("sample_pct", 100))
        timeout = int(params.get("timeout_seconds", COL_STATS_TIMEOUT))

        mgr = get_mysql_manager()

        # 1. 估算行数
        est_rows = await mgr.get_table_rows_estimate(db_name, table_name)
        sample_pct, sample_note = resolve_sample_ratio(est_rows)
        sample_pct = min(sample_pct * 100, requested_sample_pct)  # 取更保守的
        sample_pct = max(sample_pct, 1)  # 最低 1%

        # 2. 获取所有列
        all_cols = await mgr.query(db_name, _COL_TYPE_PRIORITY_SQL, (db_name, table_name))
        col_info = {c["COLUMN_NAME"]: c["DATA_TYPE"] for c in all_cols}

        # 3. 决定统计哪些列
        if requested_columns:
            target_cols = [c for c in requested_columns if c in col_info]
        else:
            # 按优先级排序，最多 MAX_COLS_WIDE_TABLE 列
            sorted_cols = sorted(
                col_info.items(),
                key=lambda x: _rank_column_priority(x[1]),
            )
            target_cols = [c[0] for c in sorted_cols[:MAX_COLS_WIDE_TABLE]]
            skipped = [c[0] for c in sorted_cols[MAX_COLS_WIDE_TABLE:]]

        if not target_cols:
            return ToolResult(success=False, message=f"在表 {table_name} 中未找到指定列", render=f"列不存在")

        # 4. 构建状态标注
        notes = []
        if sample_note:
            notes.append(sample_note)
        if not requested_columns and len(col_info) > MAX_COLS_WIDE_TABLE:
            notes.append(f"已跳过 {len(skipped)} 列: {', '.join(skipped[:10])}...")

        # 5. 计算采样偏移量
        sample_mod = max(1, int(100 / sample_pct)) if sample_pct < 100 else 1
        sample_condition = ""
        if sample_mod > 1:
            sample_condition = f" WHERE MOD(CRC32(CONCAT_WS(',', `{target_cols[0]}`)), {sample_mod}) = 0"

        # 6. 逐列统计
        total = sum(1 for _ in target_cols)
        numeric_results = {}
        categorical_results = {}
        skipped_cols = []

        for col in target_cols:
            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

            dtype = col_info[col].lower()
            is_numeric = any(t in dtype for t in ("int", "float", "double", "decimal", "numeric"))

            try:
                if is_numeric:
                    # 数值统计
                    sql = (
                        f"SELECT `{col}` FROM `{db_name}`.`{table_name}`"
                        f"{sample_condition}"
                    )
                    rows = await asyncio.wait_for(
                        mgr.query(db_name, sql),
                        timeout=timeout,
                    )
                    vals = np.array(
                        [float(r[col]) if r[col] is not None else np.nan for r in rows],
                        dtype=float,
                    )
                    numeric_results[col] = compute_numeric_stats(
                        vals, sample_pct=int(sample_pct),
                    )
                else:
                    # 类别统计
                    distinct_sql = (
                        f"SELECT DISTINCT `{col}` FROM `{db_name}`.`{table_name}`"
                        f"{sample_condition}"
                    )
                    distinct_rows = await asyncio.wait_for(
                        mgr.query(db_name, distinct_sql),
                        timeout=timeout,
                    )
                    vals = np.array([r[col] for r in distinct_rows])
                    categorical_results[col] = compute_categorical_stats(vals)
            except asyncio.TimeoutError:
                skipped_cols.append(col)
                log.warning("列 %s 统计超时 (%ds)，已跳过", col, timeout)
            except Exception as exc:
                skipped_cols.append(col)
                log.warning("列 {} 统计异常: {}", col, exc)

        # 7. 格式化输出
        lines = [f"表统计: {db_name}.{table_name} (估算 {est_rows} 行)"]
        if notes:
            lines.append("[" + "] [".join(notes) + "]")
        if skipped_cols:
            lines.append(f"[未完成 {len(skipped_cols)} 列: {', '.join(skipped_cols[:10])}]")
        lines.append("")

        if numeric_results:
            lines.append("── 数值字段 ──")
            for col_name, stats in numeric_results.items():
                lines.append(format_numeric_stats(col_name, stats))

        if categorical_results:
            lines.append("")
            lines.append("── 类别字段 ──")
            for col_name, stats in categorical_results.items():
                lines.append(format_categorical_stats(col_name, stats))

        # 8. 相关性（仅全量 + 数值列 ≤ 10）
        if sample_pct >= 100 and len(numeric_results) >= 2 and len(numeric_results) <= 10:
            lines.append("")
            lines.append("── 相关性 ──")
            # 需要取所有数值列的全量数据
            corr_cols = list(numeric_results.keys())
            cols_quoted = ", ".join(f"`{c}`" for c in corr_cols)
            try:
                corr_rows = await asyncio.wait_for(
                    mgr.query(db_name,
                              f"SELECT {cols_quoted} FROM `{db_name}`.`{table_name}`"),
                    timeout=timeout * 2,
                )
                matrix = np.array([
                    [float(r.get(c, np.nan)) for c in corr_cols]
                    for r in corr_rows
                ]).T
                corr = compute_correlation(
                    mask_nan_matrix(matrix),
                    corr_cols,
                )
                lines.append(format_correlation(corr))
            except Exception:
                lines.append("  [相关性计算失败]")
        elif len(numeric_results) >= 2:
            lines.append("")
            lines.append("[相关性需全量数据，使用 sample_pct=100 可开启]")

        lines.append("")
        lines.append(f"共统计 {len(numeric_results) + len(categorical_results)} 列")
        return ToolResult(success=True, data="\n".join(lines), render=f"统计: {db_name}.{table_name} ({len(numeric_results) + len(categorical_results)} 列)")


def mask_nan_matrix(matrix: np.ndarray) -> np.ndarray:
    """去除含 NaN 的行，返回无 NaN 的矩阵。"""
    valid = ~np.any(np.isnan(matrix), axis=0)
    return matrix[:, valid]
