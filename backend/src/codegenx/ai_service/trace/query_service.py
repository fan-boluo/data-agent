"""admin 监控查询：PG telemetry 派生（替代原 MySQL session_metrics/turn_metrics 双轨数据源）。

契约完全不变（响应模型/序列化 camelCase 均沿用 monitor/schema → trace/schema/admin.py），
数据访问 asyncpg 直连 telemetry；告警列表返回空分页（告警由 Prometheus alert 栈承接）。
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from shared import log
from shared.constants import DEFAULT_PAGE_NUM, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from shared.schema.common import PageData

from codegenx.ai_service.trace.schema.admin import (
    MonitorAlertQueryRequest,
    MonitorAlertRecordVO,
    MonitorOverviewStats,
    MonitorSessionDetail,
    MonitorSessionQueryRequest,
    MonitorSessionSummary,
    MonitorToolCallDetail,
    MonitorTurnSummary,
    TokenUsageItem,
    TokenUsageQueryRequest,
    TurnSpanNode,
    TurnSpanTree,
)
from codegenx.ai_service.utils.config import config
from db.postgres.postgres_client import get_postgres_pool

# 工具类 op_type（admin 口径的"工具调用"含语义层检索）
_TOOL_OPS = ("tool", "retrieval")


def _jload(raw: Any, default: Any) -> Any:
    """JSONB 列容错解析（asyncpg 默认返回 str）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return default


class MonitorQueryService:
    """telemetry 派生查询（无状态，方法级建连）。"""

    async def _acquire(self) -> asyncpg.Connection:
        pool = await get_postgres_pool()
        return await pool.acquire()

    async def _release(self, conn: asyncpg.Connection) -> None:
        try:
            await (await get_postgres_pool()).release(conn)
        except Exception as exc:  # 释放失败不影响结果
            log.warning("释放 PG 连接异常: {}", exc)

    # ---------------------------------------------------------------- overview

    async def get_overview(self) -> MonitorOverviewStats:
        conn = await self._acquire()
        try:
            # 会话级派生（GROUP BY 后再聚合）
            sess = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_sessions,
                       COUNT(*) FILTER (WHERE is_running)   AS running_sessions,
                       COUNT(*) FILTER (WHERE NOT is_running AND any_bad)    AS error_sessions,
                       COUNT(*) FILTER (WHERE NOT is_running AND NOT any_bad) AS success_sessions
                FROM (
                    SELECT session_id,
                           bool_or(status = 'running') AS is_running,
                           bool_or(status NOT IN ('completed','stopped')) AS any_bad
                    FROM telemetry.traces GROUP BY session_id
                ) s
                """
            )
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_turns,
                       COALESCE(AVG(duration_ms), 0)                                AS avg_turn_duration_ms,
                       COALESCE(AVG(prompt_tokens), 0)                              AS avg_context_tokens,
                       COALESCE(AVG(NULLIF(tags->>'context_usage','')::numeric), 0)  AS avg_context_usage,
                       COALESCE(SUM(total_tokens), 0)                               AS total_tokens,
                       COALESCE(SUM(COALESCE(NULLIF(tags->>'tool_calls','')::int, 0)), 0) AS total_tool_calls
                FROM telemetry.traces
                """
            )
            llm_row = await conn.fetchrow(
                """
                SELECT COALESCE(AVG(duration_ms), 0) AS avg_llm_latency
                FROM telemetry.trace_spans WHERE op_type = 'llm'
                """
            )
        finally:
            await self._release(conn)
        return MonitorOverviewStats(
            totalSessions=int(sess["total_sessions"] or 0),
            runningSessions=int(sess["running_sessions"] or 0),
            successSessions=int(sess["success_sessions"] or 0),
            errorSessions=int(sess["error_sessions"] or 0),
            totalTurns=int(row["total_turns"] or 0),
            avgTurnDurationMs=float(row["avg_turn_duration_ms"] or 0),
            avgLlmLatencyMs=float(llm_row["avg_llm_latency"] or 0),
            avgFirstTokenMs=0.0,  # telemetry 无首 token 指标，契约兜底
            avgContextTokens=float(row["avg_context_tokens"] or 0),
            avgContextTokenUsage=float(row["avg_context_usage"] or 0),
            totalToolCalls=int(row["total_tool_calls"] or 0),
            totalTokens=int(row["total_tokens"] or 0),
            openAlerts=0,       # 告警由 Prometheus alert 栈承接
        )

    # ---------------------------------------------------------------- sessions

    async def list_sessions(self, query: MonitorSessionQueryRequest) -> PageData[MonitorSessionSummary]:
        page_num, page_size = self._normalize_page(query.page_num, query.page_size)
        offset = (page_num - 1) * page_size
        where, params = self._session_filters(query)

        conn = await self._acquire()
        try:
            # 状态过滤是派生口径（GROUP BY 后 HAVING），total 用子查询包裹
            total_row = await conn.fetchval(
                f"SELECT COUNT(*) FROM (SELECT session_id FROM telemetry.traces "
                f"GROUP BY session_id {where}) _s",
                *params,
            )
            rows = await conn.fetch(
                f"""
                SELECT session_id,
                       (array_agg(app_id ORDER BY started_at DESC))[1]           AS app_id,
                       (array_agg(user_id ORDER BY started_at DESC))[1]          AS user_id,
                       (array_agg(NULLIF(model,'') ORDER BY started_at DESC))[1] AS model,
                       bool_or(status = 'running')                               AS is_running,
                       bool_and(status IN ('completed','stopped'))               AS all_success,
                       -- 轮次只统计问答 turn（event='qa'），不含 session_summary 等后台 trace
                       COUNT(*) FILTER (WHERE event = 'qa')                      AS total_turns,
                       COALESCE(SUM(prompt_tokens), 0)                           AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0)                       AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)                            AS total_tokens,
                       MIN(started_at)                                           AS started_at,
                       MAX(COALESCE(ended_at, started_at))                       AS updated_at
                FROM telemetry.traces
                GROUP BY session_id
                {where}
                ORDER BY updated_at DESC
                LIMIT $%d OFFSET $%d
                """
                % (len(params) + 1, len(params) + 2),
                *params, page_size, offset,
            )
            session_ids = [r["session_id"] for r in rows]
            span_stats = await self._span_stats_by_session(conn, session_ids)
        finally:
            await self._release(conn)

        records = []
        for r in rows:
            stat = span_stats.get(r["session_id"], {})
            status = "running" if r["is_running"] else (
                "success" if r["all_success"] else "error")
            records.append(MonitorSessionSummary(
                sessionId=r["session_id"],
                traceId=str(r["session_id"]),  # telemetry 无 session 级 trace_id，会话键即标识
                appId=str(r["app_id"] or "main"),
                userId=str(r["user_id"] or ""),
                model=str(r["model"] or ""),
                status=status,
                totalTurns=int(r["total_turns"] or 0),
                totalPromptTokens=int(r["prompt_tokens"] or 0),
                totalCompletionTokens=int(r["completion_tokens"] or 0),
                totalToolCalls=int(stat.get("tool_calls", 0)),
                totalErrors=int(stat.get("tool_errors", 0)),
                avgLlmLatencyMs=float(stat.get("llm_latency", 0.0)),
                endReason="",
                startedAt=r["started_at"],
                endedAt=r["updated_at"] if not r["is_running"] else None,
                durationMs=int((r["updated_at"] - r["started_at"]).total_seconds() * 1000)
                    if r["started_at"] else 0,
                updatedAt=r["updated_at"],
            ))
        return self._page(records, total_row, page_num, page_size)

    async def get_session_detail(self, session_id: str) -> MonitorSessionDetail | None:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        conn = await self._acquire()
        try:
            sess = await conn.fetchrow(
                """
                SELECT session_id,
                       (array_agg(app_id ORDER BY started_at DESC))[1]           AS app_id,
                       (array_agg(user_id ORDER BY started_at DESC))[1]          AS user_id,
                       (array_agg(NULLIF(model,'') ORDER BY started_at DESC))[1] AS model,
                       bool_or(status = 'running')                               AS is_running,
                       bool_and(status IN ('completed','stopped'))               AS all_success,
                       -- 轮次只统计问答 turn（event='qa'），不含 session_summary 等后台 trace
                       COUNT(*) FILTER (WHERE event = 'qa')                      AS total_turns,
                       COALESCE(SUM(prompt_tokens), 0)                           AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0)                       AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)                            AS total_tokens,
                       MIN(started_at)                                           AS started_at,
                       MAX(COALESCE(ended_at, started_at))                       AS updated_at
                FROM telemetry.traces WHERE session_id = $1 GROUP BY session_id
                """,
                normalized,
            )
            if sess is None:
                return None
            turn_rows = await conn.fetch(
                """
                SELECT trace_id, session_id, trace_id AS turn_id, status, model,
                       prompt_tokens, completion_tokens, total_tokens, tags,
                       started_at, ended_at, duration_ms,
                       ROW_NUMBER() OVER (ORDER BY started_at) AS turn_number
                FROM telemetry.traces
                -- Turn 链路只展示问答 turn，session_summary 等后台 trace 暂不纳入
                WHERE session_id = $1 AND event = 'qa' ORDER BY started_at DESC LIMIT 100
                """,
                normalized,
            )
            stat = (await self._span_stats_by_session(conn, [normalized])).get(normalized, {})
        finally:
            await self._release(conn)

        status = "running" if sess["is_running"] else ("success" if sess["all_success"] else "error")
        session_vo = MonitorSessionSummary(
            sessionId=sess["session_id"],
            traceId=str(sess["session_id"]),
            appId=str(sess["app_id"] or "main"),
            userId=str(sess["user_id"] or ""),
            model=str(sess["model"] or ""),
            status=status,
            totalTurns=int(sess["total_turns"] or 0),
            totalPromptTokens=int(sess["prompt_tokens"] or 0),
            totalCompletionTokens=int(sess["completion_tokens"] or 0),
            totalToolCalls=int(stat.get("tool_calls", 0)),
            totalErrors=int(stat.get("tool_errors", 0)),
            avgLlmLatencyMs=float(stat.get("llm_latency", 0.0)),
            endReason="",
            startedAt=sess["started_at"],
            endedAt=sess["updated_at"] if not sess["is_running"] else None,
            durationMs=int((sess["updated_at"] - sess["started_at"]).total_seconds() * 1000)
                if sess["started_at"] else 0,
            updatedAt=sess["updated_at"],
        )
        turns = [self._map_turn_row(row, stat) for row in turn_rows]
        return MonitorSessionDetail(session=session_vo, turns=turns, alerts=[])

    async def get_turn_detail(self, session_id: str, turn_id: str) -> MonitorTurnSummary | None:
        conn = await self._acquire()
        try:
            row = await conn.fetchrow(
                """
                SELECT trace_id, session_id, trace_id AS turn_id, status, model,
                       prompt_tokens, completion_tokens, total_tokens, tags,
                       started_at, ended_at, duration_ms,
                       ROW_NUMBER() OVER (ORDER BY started_at) AS turn_number
                FROM telemetry.traces
                WHERE session_id = $1 AND trace_id = $2
                LIMIT 1
                """,
                session_id, turn_id,
            )
            if row is None:
                return None
            stat = await self._span_stats_by_trace(conn, row["trace_id"])
        finally:
            await self._release(conn)
        return self._map_turn_row(row, stat, detail=True)

    # ---------------------------------------------------------------- spans 树（新）

    async def get_turn_spans(self, session_id: str, turn_id: str) -> TurnSpanTree | None:
        """turn 的扁平 span 列表 + 服务端 offsetMs（前端组树画瀑布）。"""
        conn = await self._acquire()
        try:
            trace = await conn.fetchrow(
                "SELECT trace_id, session_id, trace_id AS turn_id, started_at, duration_ms "
                "FROM telemetry.traces WHERE session_id = $1 AND trace_id = $2 LIMIT 1",
                session_id, turn_id,
            )
            if trace is None:
                return None
            spans = await conn.fetch(
                "SELECT * FROM telemetry.trace_spans WHERE trace_id = $1 ORDER BY seq",
                trace["trace_id"],
            )
        finally:
            await self._release(conn)

        t0 = trace["started_at"]
        nodes = [
            TurnSpanNode(
                spanId=s["span_id"],
                parentSpanId=s["parent_span_id"] or "",
                opType=s["op_type"] or "",
                name=s["name"] or "",
                agent=s["agent"] or "",
                seq=int(s["seq"] or 0),
                status=s["status"] or "ok",
                error=s["error"],
                startedAt=s["started_at"],
                durationMs=float(s["duration_ms"] or 0),
                offsetMs=round((s["started_at"] - t0).total_seconds() * 1000, 1) if t0 else 0.0,
                input=_jload(s["input"], None),
                output=_jload(s["output"], None),
                attributes=_jload(s["attributes"], {}),
            )
            for s in spans
        ]
        return TurnSpanTree(
            traceId=trace["trace_id"],
            sessionId=trace["session_id"],
            turnId=trace["turn_id"] or "",
            startedAt=t0,
            totalDurationMs=float(trace["duration_ms"] or 0),
            spanCount=len(nodes),
            spans=nodes,
        )

    # ---------------------------------------------------------------- alerts / tokens

    async def list_alerts(self, query: MonitorAlertQueryRequest) -> PageData[MonitorAlertRecordVO]:
        """告警由 Prometheus alert 栈承接（infra/monitoring/prometheus_alerts.yml），此处恒空分页。"""
        page_num, page_size = self._normalize_page(query.page_num, query.page_size)
        return self._page([], 0, page_num, page_size)

    async def query_token_usage(self, query: TokenUsageQueryRequest) -> PageData[TokenUsageItem]:
        page_num, page_size = self._normalize_page(query.page_num, query.page_size)
        offset = (page_num - 1) * page_size
        where, params = self._token_filters(query)

        conn = await self._acquire()
        try:
            total_row = await conn.fetchval(
                f"SELECT COUNT(*) FROM telemetry.traces {where}", *params
            )
            rows = await conn.fetch(
                f"""
                SELECT user_id, model, prompt_tokens, completion_tokens, total_tokens, ended_at
                FROM telemetry.traces {where}
                ORDER BY ended_at DESC NULLS LAST
                LIMIT $%d OFFSET $%d
                """
                % (len(params) + 1, len(params) + 2),
                *params, page_size, offset,
            )
        finally:
            await self._release(conn)

        records = [
            TokenUsageItem(
                userId=str(r["user_id"] or ""),
                modelName=str(r["model"] or "") or "unknown",
                totalPromptTokens=int(r["prompt_tokens"] or 0),
                totalCompletionTokens=int(r["completion_tokens"] or 0),
                totalTokens=int(r["total_tokens"] or 0),
                endedAt=r["ended_at"],
            )
            for r in rows
        ]
        return self._page(records, total_row, page_num, page_size)

    async def get_monitor_config(self) -> dict[str, Any]:
        """监控配置展示（agent 配置透视，只读）。"""
        return config.monitor.model_dump(mode="json", by_alias=True)

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _normalize_page(page_num: int, page_size: int) -> tuple[int, int]:
        return (max(int(page_num or DEFAULT_PAGE_NUM), 1),
                min(max(int(page_size or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE))

    @staticmethod
    def _page(records: list, total_row: int, page_num: int, page_size: int):
        return PageData(
            records=records,
            pageNumber=page_num,
            pageSize=page_size,
            totalPage=(int(total_row) + page_size - 1) // page_size if total_row else 0,
            totalRow=int(total_row or 0),
        )

    @staticmethod
    def _session_filters(query: MonitorSessionQueryRequest) -> tuple[str, list[Any]]:
        """会话过滤条件（GROUP BY 后 HAVING：聚合口径的派生状态/属性过滤）。"""
        clauses, params = [], []
        if query.status:  # 派生状态过滤（running/其他）
            if query.status == "running":
                clauses.append("bool_or(status = 'running')")
            else:
                clauses.append("bool_and(status <> 'running')")
        if query.app_id:
            clauses.append("bool_or(app_id = $%d)" % (len(params) + 1)); params.append(query.app_id)
        if query.user_id:
            clauses.append("bool_or(user_id = $%d)" % (len(params) + 1)); params.append(query.user_id)
        if query.session_id:
            clauses.append("session_id LIKE $%d" % (len(params) + 1)); params.append(f"%{query.session_id}%")
        if query.trace_id:
            clauses.append("bool_or(trace_id LIKE $%d)" % (len(params) + 1)); params.append(f"%{query.trace_id}%")
        return (f"HAVING {' AND '.join(clauses)}" if clauses else "", params)

    @staticmethod
    def _token_filters(query: TokenUsageQueryRequest) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if query.user_id:
            clauses.append("user_id = $%d" % (len(params) + 1)); params.append(query.user_id)
        if query.start_time:
            clauses.append("ended_at >= $%d" % (len(params) + 1)); params.append(query.start_time)
        if query.end_time:
            clauses.append("ended_at <= $%d" % (len(params) + 1)); params.append(query.end_time)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    @staticmethod
    async def _span_stats_by_session(conn: asyncpg.Connection, session_ids: list[str]) -> dict[str, dict]:
        """会话级 span 聚合：工具调用/错误计数 + LLM 平均延迟。"""
        if not session_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT session_id,
                   COUNT(*) FILTER (WHERE op_type = ANY($2::text[])) AS tool_calls,
                   COUNT(*) FILTER (WHERE op_type = ANY($2::text[]) AND status = 'error') AS tool_errors,
                   COALESCE(AVG(duration_ms) FILTER (WHERE op_type = 'llm'), 0) AS llm_latency
            FROM telemetry.trace_spans
            WHERE session_id = ANY($1::text[])
            GROUP BY session_id
            """,
            session_ids, list(_TOOL_OPS),
        )
        return {
            r["session_id"]: {
                "tool_calls": int(r["tool_calls"] or 0),
                "tool_errors": int(r["tool_errors"] or 0),
                "llm_latency": float(r["llm_latency"] or 0),
            } for r in rows
        }

    @staticmethod
    async def _span_stats_by_trace(conn: asyncpg.Connection, trace_id: str) -> dict[str, Any]:
        """单 trace 的 span 聚合：工具明细/计数 + LLM 延迟。"""
        rows = await conn.fetch(
            """
            SELECT name,
                   COUNT(*) AS call_count,
                   COALESCE(AVG(duration_ms), 0) AS avg_latency,
                   BOOL_OR(status = 'error') AS has_error
            FROM telemetry.trace_spans
            WHERE trace_id = $1 AND op_type = ANY($2::text[])
            GROUP BY name ORDER BY call_count DESC
            """,
            trace_id, list(_TOOL_OPS),
        )
        llm_latency = await conn.fetchval(
            "SELECT COALESCE(AVG(duration_ms), 0) FROM telemetry.trace_spans "
            "WHERE trace_id = $1 AND op_type = 'llm'",
            trace_id,
        )
        tool_details = [
            MonitorToolCallDetail(
                name=r["name"] or "",
                latencyMs=int(r["avg_latency"] or 0),
                status="error" if r["has_error"] else "success",
                callCount=int(r["call_count"] or 0),
            ) for r in rows
        ]
        return {
            "tool_details": tool_details,
            "tool_calls": sum(d.call_count for d in tool_details),
            "tool_errors": sum(d.call_count for d in tool_details if d.status == "error"),
            "llm_latency": float(llm_latency or 0),
        }

    @classmethod
    def _map_turn_row(cls, row: asyncpg.Record, stat: dict[str, Any], *, detail: bool = False) -> MonitorTurnSummary:
        tags = _jload(row["tags"], {}) or {}
        return MonitorTurnSummary(
            traceId=row["trace_id"],
            sessionId=row["session_id"],
            turnId=row["turn_id"] or "",
            turnNumber=int(row["turn_number"] or 0),
            status=row["status"] or "running",
            promptTokens=int(row["prompt_tokens"] or 0),
            completionTokens=int(row["completion_tokens"] or 0),
            llmLatencyMs=int(stat.get("llm_latency", 0.0)),
            toolCallsCount=int(stat.get("tool_calls", 0)),
            toolCallsDetail=stat.get("tool_details", []) if detail else [],
            contextTokens=int(row["prompt_tokens"] or 0),
            contextTokenUsage=int(float(tags.get("context_usage") or 0) * 100),
            errorCount=int(stat.get("tool_errors", 0)),
            startedAt=row["started_at"],
            endedAt=row["ended_at"],
            durationMs=int(row["duration_ms"] or 0),
            createdAt=row["started_at"],
        )


_MONITOR_QUERY_SERVICE_SINGLETON: MonitorQueryService | None = None


def get_monitor_query_service() -> MonitorQueryService:
    global _MONITOR_QUERY_SERVICE_SINGLETON
    if _MONITOR_QUERY_SERVICE_SINGLETON is None:
        _MONITOR_QUERY_SERVICE_SINGLETON = MonitorQueryService()
    return _MONITOR_QUERY_SERVICE_SINGLETON
