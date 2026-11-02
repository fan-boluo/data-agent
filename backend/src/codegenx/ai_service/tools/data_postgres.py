"""
PostgreSQL 只读 SQL 执行工具（data_explore 智能体的核心工具）。

db_name / db_schema 由 ToolExecutor 从 session_state.current_db 注入
（current_db 格式 "datasource.schema"，如 business.dvdrental）。
SQL 经只读守卫后执行，结果格式化为文本表格（最多展示 50 行）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from codegenx.ai_service.utils.dbutils import check_sql_safety
from codegenx.ai_service.tools.base import BaseTool, ToolResult
from codegenx.ai_service.utils.dbutils import format_rows
from shared import log, settings


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
        db_name = str(params.get("db_name") or "").strip()
        if not db_name:
            return ToolResult(
                success=False,
                message="未选择数据集，无法执行 SQL；请先让用户选择要分析的数据集",
            )
        schema = str(params.get("db_schema") or "").strip()

        # ── 统一只读守卫（复用 dbutils 安全检查） ──
        check_sql_safety(sql)

        from codegenx.ai_service.agent.db import postgres_manager
        rows = await postgres_manager.query(db_name, sql, schema=schema)
        row_count = len(rows)

        output = f"查询结果（共 {row_count} 行）:\n" + format_rows(rows)
        # extras.rows_preview 供分析链路 hook 提取展示（前 10 行原始 dict，不进 LLM 上下文）
        extras = {"rows_preview": rows[:10]} if rows else {}
        return ToolResult(success=True, data=output, render=f"SQL 执行成功（{row_count} 行）", extras=extras)
