"""
PostgreSQL 数据分析工具集。

db_name / db_schema 由工具凭 session_id 解析当前数据集（不再由执行器注入）
（current_db 格式 "datasource.schema"，如 business.dvdrental）。

工具分工：
  - execute_postgresql  核心：任意只读 SQL 执行
  - list_tables         冷启动自探：列出 schema 下表 + 行数估算
  - describe_table      表结构精读：列/类型/主键/索引/行数
  - sample_rows         数据采样：TABLESAMPLE 或哈希采样
  - describe_table_stats 运行时统计：数值/类别/相关性（复用 stats_engine）

PostgreSQL 方言要点：
  - 采样优先 TABLESAMPLE SYSTEM(n)，降级用哈希模运算
  - 行数估算 pg_class.reltuples（ANALYZE 后最准）
  - 信息查询走 pg_catalog / information_schema 只读视图
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from codegenx.ai_service.utils.dbutils import check_sql_safety
from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from codegenx.ai_service.utils.dbutils import format_rows
from shared import log, settings

# 标识符安全校验：只允许 PG 合法标识符字符（双引号内可多字符，这里严格一些防注入）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# 宽表列数上限（超过则只统计前 N 列，其余标注跳过）
MAX_COLS_WIDE_TABLE = 20
# 单列统计超时（秒）
COL_STATS_TIMEOUT = 30


class ExecutePostgresqlTool(BaseTool):

    @property
    def name(self) -> str:
        return "execute_postgresql"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "在当前公共数据仓库（PostgreSQL）上执行一条任意只读 SQL 查询，返回结果表格（最多展示前 50 行）。"
            "仅允许 SELECT/SHOW/DESC/EXPLAIN/WITH 语句，禁止写操作与 DDL。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "完整可执行的只读 PostgreSQL 语句",
                },
            },
            "required": ["sql"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("execute_postgresql 执行异常: {}", exc)
            return ToolResult(success=False, message=f"SQL 执行失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        sql = str(params.get("sql") or "").strip()
        if not sql:
            return ToolResult(success=False, message="SQL 为空，无法执行")
        sess = await ToolSession.load(params)
        db_name = str(params.get("db_name") or sess.db_name).strip()
        if not db_name:
            return ToolResult(
                success=False,
                message="未选择数据集，无法执行 SQL；请先让用户选择要分析的数据集",
            )
        schema = str(params.get("db_schema") or sess.db_schema).strip()

        # ── 统一只读守卫（复用 dbutils 安全检查） ──
        check_sql_safety(sql)

        from codegenx.ai_service.agent.db import postgres_manager
        rows = await postgres_manager.query(db_name, sql, schema=schema)
        row_count = len(rows)

        output = f"查询结果（共 {row_count} 行）:\n" + format_rows(rows)
        # extras.rows_preview 供分析链路 hook 提取展示（前 10 行原始 dict，不进 LLM 上下文）
        extras = {"rows_preview": rows[:10]} if rows else {}
        return ToolResult(success=True, data=output, render=f"SQL 执行成功（{row_count} 行）", extras=extras)


# ═══════════════════════════════════════════════════════════
# 辅助函数：标识符安全 + 表引用解析
# ═══════════════════════════════════════════════════════════

def _quote_ident(name: str) -> str:
    """双引号包裹标识符（schema 名/表名/列名），防注入。"""
    _IDENT_RE.match(name) or _IDENT_RE.match(name)  # 触发校验，非法名直接抛
    return f'"{name}"'


def _parse_table_ref(raw: str) -> tuple[str | None, str]:
    """解析表引用：schema.table 或 table，返回 (schema, table)。"""
    parts = [p for p in (raw or "").split(".") if p]
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return None, parts[0]
    return None, ""


# PostgreSQL information_schema 查询（带 schema 过滤）
_COLUMNS_SQL = (
    "SELECT column_name, data_type, udt_name, character_maximum_length, "
    "numeric_precision, numeric_scale, is_nullable, column_default, "
    "COALESCE(col_description(attrelid::regclass, ordinal_position), '') AS column_comment "
    "FROM information_schema.columns c "
    "JOIN pg_catalog.pg_attribute a ON a.attname = c.column_name "
    "JOIN pg_catalog.pg_class cls ON cls.relname = c.table_name "
    "JOIN pg_catalog.pg_namespace n ON n.oid = cls.relnamespace AND n.nspname = c.table_schema "
    "WHERE c.table_name = $1 {schema_filter} "
    "ORDER BY c.ordinal_position"
)

_TABLE_COMMENT_SQL = (
    "SELECT COALESCE(obj_description('{schema_filter_clause}'::regclass), '') AS table_comment"
)

_INDEX_SQL = (
    "SELECT i.relname AS index_name, idx.indisunique AS is_unique, "
    "array_to_string(array(SELECT a.attname FROM pg_catalog.pg_attribute a "
    "  WHERE a.attrelid = idx.indrelid AND a.attnum = ANY(idx.indkey) "
    "  AND NOT a.attisdropped ORDER BY array_position(idx.indkey, a.attnum)), ', ') AS columns, "
    "idx.indisprimary AS is_primary, am.amname AS type "
    "FROM pg_catalog.pg_index idx "
    "JOIN pg_catalog.pg_class t ON t.oid = idx.indrelid "
    "JOIN pg_catalog.pg_class i ON i.oid = idx.indexrelid "
    "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
    "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace {schema_filter} "
    "WHERE t.relname = $1"
)


# ═══════════════════════════════════════════════════════════
# 2. list_tables — 列出 schema 下所有表（带行数估算）
# ═══════════════════════════════════════════════════════════

_LIST_TABLES_SQL_NO_SCHEMA = (
    "SELECT c.relname AS table_name, "
    "  COALESCE(obj_description(c.oid), '') AS comment, "
    "  c.reltuples::bigint AS estimated_rows "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND c.relname NOT LIKE 'pg_%' "
    "ORDER BY c.relname"
)

_LIST_TABLES_SQL_WITH_SCHEMA = (
    "SELECT c.relname AS table_name, "
    "  COALESCE(obj_description(c.oid), '') AS comment, "
    "  c.reltuples::bigint AS estimated_rows "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind = 'r' AND n.nspname = $1 "
    "ORDER BY c.relname"
)


class ListTablesTool(BaseTool):

    @property
    def name(self) -> str:
        return "list_tables"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "列出当前数据集 schema 下所有表的基本信息：表名、表注释、行数估算。"
            "用于新库冷启动时了解有哪些表，或确认 schema 是否已被语义层采集。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("list_tables 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取表列表失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        sess = await ToolSession.load(params)
        db_name = str(params.get("db_name") or sess.db_name).strip()
        if not db_name:
            return ToolResult(success=False, message="未选择数据集，请先选择要分析的数据集")
        schema = str(params.get("db_schema") or sess.db_schema).strip()

        from codegenx.ai_service.agent.db import postgres_manager
        # 用 postgres_manager.query 走连接池 + search_path 模式
        # 因为 list_tables SQL 不含占位参数，直接字符串拼 schema_filter 也行
        if schema:
            sql = (
                "SELECT c.relname AS table_name, "
                "  COALESCE(obj_description(c.oid), '') AS comment, "
                "  c.reltuples::bigint AS estimated_rows "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE c.relkind = 'r' AND n.nspname = '{schema}' "
                "ORDER BY c.relname"
            )
        else:
            sql = _LIST_TABLES_SQL_NO_SCHEMA
        rows = await postgres_manager.query(db_name, sql, schema=schema)

        if not rows:
            return ToolResult(
                success=True,
                data=f"schema {schema or db_name} 中没有表",
                render=f"schema {schema}: 无表",
            )

        # 规整 estimated_rows（pg_class 给的是 0 或负数，标注成"未知"）
        for r in rows:
            est = r.get("estimated_rows") or 0
            r["estimated_rows"] = f"~{est}" if est > 0 else "未知"

        schema_label = schema or "全部"
        output = f"schema {schema_label} 共有 {len(rows)} 张表:\n"
        output += format_rows(rows)
        return ToolResult(
            success=True, data=output,
            render=f"schema {schema_label}: {len(rows)} 张表",
        )


# ═══════════════════════════════════════════════════════════
# 3. describe_table — 表结构精读（列/类型/主键/索引/行数）
# ═══════════════════════════════════════════════════════════

class DescribeTableTool(BaseTool):

    @property
    def name(self) -> str:
        return "describe_table"

    @property
    def label(self) -> str:
        return "data_analysis"

    @property
    def description(self) -> str:
        return (
            "返回指定表的详细结构信息：列名、数据类型、可空、默认值、注释、"
            "主键与索引、行数估算。用于理解表结构后再编写 SQL。"
            "表名可写 schema.table，不填 schema 则用当前数据集默认 schema。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "表名，可写 schema.table",
                },
            },
            "required": ["table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_table 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取表结构失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        sess = await ToolSession.load(params)
        db_name = str(params.get("db_name") or sess.db_name).strip()
        if not db_name:
            return ToolResult(success=False, message="未选择数据集，请先选择要分析的数据集")

        # 表引用解析：schema.table / table
        schema_from_ref, table = _parse_table_ref(str(params.get("table_name") or "").strip())
        if not table:
            return ToolResult(success=False, message="请提供有效的表名")
        schema = schema_from_ref or str(params.get("db_schema") or sess.db_schema).strip()

        from codegenx.ai_service.agent.db import postgres_manager
        # 并行查：列信息、索引、表注释、行数估算
        cols_sql = _build_columns_sql(table, schema)
        index_sql = _build_index_sql(table, schema)
        comment_sql = _build_table_comment_sql(table, schema)

        cols_task = postgres_manager.query(db_name, cols_sql, schema=schema)
        index_task = postgres_manager.query(db_name, index_sql, schema=schema)
        comment_task = postgres_manager.query(db_name, comment_sql, schema=schema)
        est_rows_task = postgres_manager.get_table_rows_estimate(db_name, table, schema=schema)

        columns, indexes, comment_rows, est_rows = await asyncio.gather(
            cols_task, index_task, comment_task, est_rows_task,
        )

        comment = comment_rows[0]["table_comment"] if comment_rows else ""
        schema_label = schema or "默认"
        n_cols = len(columns)

        lines = [f"表: {schema_label}.{table}"]
        if comment:
            lines.append(f"注释: {comment}")
        lines.append(f"列数: {n_cols} | 估算行数: {est_rows or '未知'}")
        lines.append("")

        # 列信息
        if columns:
            lines.append("── 列信息 ──")
            lines.append(format_rows(columns))
        else:
            lines.append("（未找到列信息，可能表不存在或 schema 不对）")

        # 索引
        if indexes:
            lines.append("")
            lines.append("── 索引 ──")
            # 归一化 is_primary / is_unique 可读化
            for idx in indexes:
                idx["is_primary"] = "主键" if idx.get("is_primary") else ""
                idx["is_unique"] = "唯一" if idx.get("is_unique") else ""
            lines.append(format_rows(indexes))

        return ToolResult(
            success=True, data="\n".join(lines),
            render=f"表结构: {schema_label}.{table} ({n_cols} 列)",
        )


def _build_columns_sql(table: str, schema: str) -> str:
    """构造查列的 SQL（用 regclass 处理 schema 限定）。"""
    # col_description 需要对象 OID，通过表名 + schema 双保险
    table_ident = _quote_ident(table)
    if schema:
        schema_ident = _quote_ident(schema)
        table_ref = f"{schema_ident}.{table_ident}"
    else:
        table_ref = table_ident
    # 用 $1 占位表名（asyncpg 参数化），schema 通过 query 的 search_path 生效
    schema_filter = f"AND c.table_schema = '{schema}'" if schema else "AND c.table_schema NOT IN ('pg_catalog', 'information_schema')"
    return (
        "SELECT "
        "column_name, "
        "data_type, "
        "CASE udt_name WHEN 'varchar' THEN data_type || '(' || character_maximum_length || ')' "
        "  WHEN 'numeric' THEN data_type || '(' || numeric_precision || ',' || numeric_scale || ')' "
        "  ELSE data_type END AS type_display, "
        "is_nullable AS nullable, "
        "column_default AS default_value, "
        "COALESCE(col_description('" + table_ref + "'::regclass, ordinal_position), '') AS column_comment "
        "FROM information_schema.columns "
        f"WHERE table_name = '{table}' {schema_filter} "
        "ORDER BY ordinal_position"
    )


def _build_index_sql(table: str, schema: str) -> str:
    """构造查索引的 SQL。"""
    if schema:
        return (
            "SELECT i.relname AS index_name, "
            "array_to_string(array(SELECT a.attname FROM pg_catalog.pg_attribute a "
            "  WHERE a.attrelid = idx.indrelid AND a.attnum = ANY(idx.indkey) "
            "  AND NOT a.attisdropped ORDER BY array_position(idx.indkey, a.attnum)), ', ') AS columns, "
            "idx.indisunique AS is_unique, idx.indisprimary AS is_primary, am.amname AS type "
            "FROM pg_catalog.pg_index idx "
            "JOIN pg_catalog.pg_class t ON t.oid = idx.indrelid "
            "JOIN pg_catalog.pg_class i ON i.oid = idx.indexrelid "
            "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            f"WHERE t.relname = '{table}' AND n.nspname = '{schema}'"
        )
    return (
        "SELECT i.relname AS index_name, "
        "array_to_string(array(SELECT a.attname FROM pg_catalog.pg_attribute a "
        "  WHERE a.attrelid = idx.indrelid AND a.attnum = ANY(idx.indkey) "
        "  AND NOT a.attisdropped ORDER BY array_position(idx.indkey, a.attnum)), ', ') AS columns, "
        "idx.indisunique AS is_unique, idx.indisprimary AS is_primary, am.amname AS type "
        "FROM pg_catalog.pg_index idx "
        "JOIN pg_catalog.pg_class t ON t.oid = idx.indrelid "
        "JOIN pg_catalog.pg_class i ON i.oid = idx.indexrelid "
        "JOIN pg_catalog.pg_am am ON am.oid = i.relam "
        f"WHERE t.relname = '{table}'"
    )


def _build_table_comment_sql(table: str, schema: str) -> str:
    """构造查表注释的 SQL。"""
    if schema:
        table_ref = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    else:
        table_ref = _quote_ident(table)
    return f"SELECT COALESCE(obj_description('{table_ref}'::regclass), '') AS table_comment"


# ═══════════════════════════════════════════════════════════
# 4. sample_rows — TABLESAMPLE 采样（禁止 RANDOM() 全表扫描）
# ═══════════════════════════════════════════════════════════

class SampleRowsTool(BaseTool):

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
            "内部使用 PostgreSQL TABLESAMPLE SYSTEM 避免全表扫描；大表采样效率高。"
            "长文本会被截断到 200 字符。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "表名，可写 schema.table",
                },
                "limit": {
                    "type": "integer",
                    "description": "采样行数，默认 10，最大 100",
                },
            },
            "required": ["table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sample_rows 执行异常: {}", exc)
            return ToolResult(success=False, message=f"采样数据失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        sess = await ToolSession.load(params)
        db_name = str(params.get("db_name") or sess.db_name).strip()
        if not db_name:
            return ToolResult(success=False, message="未选择数据集，请先选择要分析的数据集")

        schema_from_ref, table = _parse_table_ref(str(params.get("table_name") or "").strip())
        if not table:
            return ToolResult(success=False, message="请提供有效的表名")
        schema = schema_from_ref or str(params.get("db_schema") or sess.db_schema).strip()

        limit = int(params.get("limit", 10))
        if limit < 1:
            limit = 10
        if limit > 100:
            limit = 100

        from codegenx.ai_service.agent.db import postgres_manager

        # 先拿列清单（需列名用于 SELECT）
        col_sql = _build_columns_sql(table, schema)
        try:
            cols_info = await postgres_manager.query(db_name, col_sql, schema=schema)
        except Exception as exc:
            return ToolResult(success=False, message=f"表不存在或无权限: {exc}")

        col_names = [c["column_name"] for c in cols_info]
        if not col_names:
            return ToolResult(success=False, message=f"表 {table} 无列或不存在")

        cols_quoted = ", ".join(_quote_ident(c) for c in col_names)
        table_ref = f"{_quote_ident(schema)}.{_quote_ident(table)}" if schema else _quote_ident(table)

        # 优先 TABLESAMPLE SYSTEM(n)：物理块采样，百万级大表也很快
        # SYSTEM 采样返回行数 ≈ total_rows * n%，最后用 LIMIT 收束
        est_rows = await postgres_manager.get_table_rows_estimate(db_name, table, schema=schema)
        if est_rows > 10_000:
            # 大表：用 5% SYSTEM 采样（最多返回约 5% 行，LIMIT 收束到 limit）
            sample_pct = 5
        else:
            # 小表：全量取前 limit 行，不用采样
            sample_pct = 100

        if sample_pct < 100:
            sql = (
                f"SELECT {cols_quoted} FROM {table_ref} "
                f"TABLESAMPLE SYSTEM({sample_pct}) LIMIT {limit}"
            )
        else:
            sql = f"SELECT {cols_quoted} FROM {table_ref} LIMIT {limit}"

        try:
            rows = await postgres_manager.query(db_name, sql, schema=schema)
        except Exception as exc:
            log.warning("TABLESAMPLE 采样失败，降级为全量 LIMIT: {}", exc)
            rows = await postgres_manager.query(
                db_name,
                f"SELECT {cols_quoted} FROM {table_ref} LIMIT {limit}",
                schema=schema,
            )

        schema_label = schema or "默认"
        output = f"表 {schema_label}.{table} 采样 {len(rows)} 行:\n"
        output += format_rows(rows)
        sample_note = f"（TABLESAMPLE {sample_pct}%）" if sample_pct < 100 else ""
        return ToolResult(
            success=True, data=output,
            render=f"采样: {schema_label}.{table} ({len(rows)} 行){sample_note}",
        )


# ═══════════════════════════════════════════════════════════
# 5. describe_table_stats — 数值/类别/相关性统计
# ═══════════════════════════════════════════════════════════

# 列优先级：数值 > 时间 > 日期 > 低基数字符串
def _rank_column_priority(data_type: str) -> int:
    dt = data_type.lower()
    if any(t in dt for t in ("int", "float", "double", "decimal", "numeric", "real", "bigint", "smallint", "integer")):
        return 1  # 数值列最高优先
    if any(t in dt for t in ("timestamp", "date", "time")):
        return 2  # 时间列
    if any(t in dt for t in ("varchar", "char", "text", "uuid", "jsonb", "json")):
        return 3  # 字符串/复杂类型
    return 4


class DescribeTableStatsTool(BaseTool):

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
            "类别字段的频率分布，缺失值，列间相关性提示。"
            "大表会自动 TABLESAMPLE 降级采样并标注，超宽表自动选重点列（数值 > 时间 > 字符串）。"
            "可通过 columns 指定要统计的列。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "表名，可写 schema.table",
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
            },
            "required": ["table_name"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("describe_table_stats 执行异常: {}", exc)
            return ToolResult(success=False, message=f"获取统计信息失败: {exc}")

    async def _do_execute(self, params: dict) -> ToolResult:
        from codegenx.ai_service.data_analysis.stats_engine import (
            compute_numeric_stats, compute_categorical_stats,
            compute_correlation, format_numeric_stats,
            format_categorical_stats, format_correlation,
            _resolve_sample_pct,
        )

        sess = await ToolSession.load(params)
        db_name = str(params.get("db_name") or sess.db_name).strip()
        if not db_name:
            return ToolResult(success=False, message="未选择数据集，请先选择要分析的数据集")

        schema_from_ref, table = _parse_table_ref(str(params.get("table_name") or "").strip())
        if not table:
            return ToolResult(success=False, message="请提供有效的表名")
        schema = schema_from_ref or str(params.get("db_schema") or sess.db_schema).strip()

        requested_columns = params.get("columns")
        requested_sample_pct = int(params.get("sample_pct", 100))

        from codegenx.ai_service.agent.db import postgres_manager

        # 1. 估算行数 → 自动决定采样率
        est_rows = await postgres_manager.get_table_rows_estimate(db_name, table, schema=schema)
        auto_pct, _is_sampled = _resolve_sample_pct(est_rows, requested_sample_pct)
        # 取两者更保守的（auto_pct 基于阈值，requested 基于用户指定）
        sample_pct = min(auto_pct, requested_sample_pct)
        sample_pct = max(sample_pct, 1)

        # 2. 获取所有列信息（类型）
        cols_sql = _build_columns_sql(table, schema)
        try:
            cols_info = await postgres_manager.query(db_name, cols_sql, schema=schema)
        except Exception as exc:
            return ToolResult(success=False, message=f"表不存在或无权限: {exc}")

        col_info: dict[str, str] = {c["column_name"]: c["data_type"].lower() for c in cols_info}
        if not col_info:
            return ToolResult(success=False, message=f"表 {table} 无列")

        # 3. 决定统计哪些列
        if requested_columns:
            target_cols = [c for c in requested_columns if c in col_info]
            skipped = []
        else:
            sorted_cols = sorted(col_info.items(), key=lambda x: _rank_column_priority(x[1]))
            target_cols = [c[0] for c in sorted_cols[:MAX_COLS_WIDE_TABLE]]
            skipped = [c[0] for c in sorted_cols[MAX_COLS_WIDE_TABLE:]]

        if not target_cols:
            return ToolResult(success=False, message=f"在表 {table} 中未找到指定列")

        # 4. 构建状态标注
        notes = []
        if sample_pct < 100:
            notes.append(f"TABLESAMPLE {sample_pct}%，全量约 {est_rows} 行")
        if not requested_columns and len(col_info) > MAX_COLS_WIDE_TABLE:
            notes.append(f"已跳过 {len(skipped)} 列: {', '.join(skipped[:10])}...")

        # 5. 逐列统计：先用 TABLESAMPLE 取样本 → 再对样本按列聚合统计
        table_ref = f"{_quote_ident(schema)}.{_quote_ident(table)}" if schema else _quote_ident(table)

        # 数值列 vs 类别列分类（基于 data_type）
        def _is_numeric(dtype: str) -> bool:
            return any(t in dtype for t in (
                "int", "float", "double", "decimal", "numeric",
                "real", "bigint", "smallint", "integer", "money",
            ))

        numeric_results: dict[str, dict] = {}
        categorical_results: dict[str, dict] = {}
        incomplete: list[str] = []

        # 一次取所有目标列的样本数据（TABLESAMPLE 快），再在内存里逐列统计
        cols_quoted = ", ".join(_quote_ident(c) for c in target_cols)
        if sample_pct < 100:
            sample_sql = (
                f"SELECT {cols_quoted} FROM {table_ref} "
                f"TABLESAMPLE SYSTEM({sample_pct})"
            )
        else:
            sample_sql = f"SELECT {cols_quoted} FROM {table_ref}"

        try:
            sample_rows = await asyncio.wait_for(
                postgres_manager.query(db_name, sample_sql, schema=schema),
                timeout=COL_STATS_TIMEOUT * 4,  # TABLESAMPLE 取数超时放大
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                message=f"TABLESAMPLE 取数超时（>{COL_STATS_TIMEOUT * 4}s），请手动指定更小的 sample_pct",
            )

        import numpy as np
        for col in target_cols:
            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

            try:
                dtype = col_info[col]
                vals = np.array([r.get(col) for r in sample_rows])
                if _is_numeric(dtype):
                    converted = np.array(
                        [float(v) if v is not None else np.nan for v in vals],
                        dtype=float,
                    )
                    numeric_results[col] = compute_numeric_stats(converted, sample_pct)
                else:
                    categorical_results[col] = compute_categorical_stats(vals)
            except Exception as exc:
                incomplete.append(col)
                log.warning("列 {} 统计异常: {}", col, exc)

        # 6. 相关性（仅全量 + 数值列 ≤ 15）
        corr_section = ""
        if sample_pct >= 100 and len(numeric_results) >= 2 and len(numeric_results) <= 15:
            try:
                num_cols = list(numeric_results.keys())
                matrix = np.array([
                    [float(r.get(c, np.nan)) if r.get(c) is not None else np.nan for c in num_cols]
                    for r in sample_rows
                ]).T
                # 去除含 NaN 的行
                valid = ~np.any(np.isnan(matrix), axis=0)
                matrix = matrix[:, valid]
                corr = compute_correlation(matrix, num_cols)
                corr_section = "\n\n── 相关性 ──\n" + format_correlation(corr)
            except Exception:
                corr_section = "\n\n[相关性计算失败]"
        elif len(numeric_results) >= 2:
            corr_section = "\n\n[相关性需全量数据，使用 sample_pct=100 可开启]"

        # 7. 格式化输出
        schema_label = schema or "默认"
        lines = [f"表统计: {schema_label}.{table} (估算 {est_rows} 行)"]
        if notes:
            lines.append("[" + "] [".join(notes) + "]")
        if incomplete:
            lines.append(f"[未完成 {len(incomplete)} 列: {', '.join(incomplete[:10])}]")
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

        lines.append(corr_section)
        lines.append("")
        n_stats = len(numeric_results) + len(categorical_results)
        lines.append(f"共统计 {n_stats} 列")
        return ToolResult(
            success=True, data="\n".join(lines),
            render=f"统计: {schema_label}.{table} ({n_stats} 列)",
        )
