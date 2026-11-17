
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from codegenx.ai_service.system_app import get_app
from codegenx.gateway.middleware.auth import require_role
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from codegenx.ai_service.trace.maintenance import get_monitor_maintenance_service
from codegenx.ai_service.trace.query_service import get_monitor_query_service
from codegenx.user_service.user_enums import UserRole
from codegenx.ai_service.trace.schema.admin import (
    MonitorAlertQueryRequest,
    MonitorSessionQueryRequest,
    TokenUsageQueryRequest,
)
from shared import log
from shared.exceptions.business_exception import BusinessException
from shared.exceptions.error_code import ErrorCode
from shared.utils.result_utils import success
# ── 监控 / 统计（原 /internal 端点，改挂 /api/stats/admin 并收紧为管理员） ──

monitor_router = APIRouter(prefix="/stats/admin", tags=["monitor"], dependencies=[Depends(require_role(UserRole.ADMIN))])


@monitor_router.get("/monitor/overview")
async def internal_get_monitor_overview():
    try:
        result = await get_monitor_query_service().get_overview()
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor overview failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/pool")
async def internal_get_session_pool():
    """当前 SessionPool 概览：状态分布 + 会话明细（运行追踪页数据源）。"""
    try:
        pool = get_app().session_pool
        sessions = await pool.snapshot()
        stats = {
            "totalSessions": len(sessions),
            "runningSessions": sum(1 for item in sessions if item["status"] == "running"),
            "idleSessions": sum(1 for item in sessions if item["status"] == "idle"),
            "closedSessions": sum(1 for item in sessions if item["status"] == "closed"),
            "maxSessions": pool.max_sessions,
        }
        return success({"stats": stats, "sessions": sessions})
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor session pool failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/sessions/all")
async def internal_list_all_sessions(
    page_num: int = Query(default=1, alias="pageNum"),
    page_size: int = Query(default=10, alias="pageSize"),
    status: str | None = None,
    app_id: str | None = Query(default=None, alias="appId"),
    user_id: str | None = Query(default=None, alias="userId"),
    session_id: str | None = Query(default=None, alias="sessionId"),
):
    """全量会话检索：池内会话 + 历史会话合并；不在池中的统一标记为 closed。

    管理端展示上限 2000 条，避免一次扫全表；超出部分后续可改游标分页。
    """
    try:
        pool_rows = await get_app().session_pool.snapshot()
        pool_map = {row["sessionId"]: row for row in pool_rows}

        history = await get_monitor_query_service().list_sessions(
            MonitorSessionQueryRequest(
                pageNum=1,
                pageSize=2000,
                appId=app_id,
                userId=user_id,
                sessionId=session_id,
            )
        )
        merged: dict[str, dict] = {}
        for rec in history.records:
            # API 出参为 camelCase 别名，模型内部属性仍是 snake_case
            pool_row = pool_map.get(rec.session_id) or {}
            in_pool = rec.session_id in pool_map
            merged[rec.session_id] = {
                "sessionId": rec.session_id,
                "appId": rec.app_id,
                "userId": rec.user_id,
                "status": pool_row.get("status", "idle") if in_pool else "closed",
                "inPool": in_pool,
                "totalTurns": rec.total_turns,
                "messageCount": pool_row.get("messageCount", 0),
                "lastActivityAt": pool_row.get("lastActivityAt")
                or (rec.updated_at.isoformat() if rec.updated_at else None),
                "idleSeconds": pool_row.get("idleSeconds", 0),
                "pendingRequests": pool_row.get("pendingRequests", 0),
            }
        # 池内但尚无 trace 的会话也要能看到
        for sid, pool_row in pool_map.items():
            if sid in merged:
                continue
            merged[sid] = {
                "sessionId": sid,
                "appId": pool_row.get("appId", ""),
                "userId": pool_row.get("userId", ""),
                "status": pool_row.get("status", "idle"),
                "inPool": True,
                "totalTurns": 0,
                "messageCount": pool_row.get("messageCount", 0),
                "lastActivityAt": pool_row.get("lastActivityAt"),
                "idleSeconds": pool_row.get("idleSeconds", 0),
                "pendingRequests": pool_row.get("pendingRequests", 0),
            }

        rows = list(merged.values())
        if app_id:
            rows = [r for r in rows if app_id in r["appId"]]
        if user_id:
            rows = [r for r in rows if user_id in r["userId"]]
        if session_id:
            rows = [r for r in rows if session_id in r["sessionId"]]
        if status:
            rows = [r for r in rows if r["status"] == status]

        # 先按活跃时间倒序，再按状态分组（running → idle → closed）
        rows.sort(key=lambda r: str(r["lastActivityAt"] or ""), reverse=True)
        priority = {"running": 0, "idle": 1, "closed": 2}
        rows.sort(key=lambda r: priority.get(r["status"], 3))

        page_size = max(1, min(int(page_size), 100))
        page_num = max(1, int(page_num))
        total_row = len(rows)
        start = (page_num - 1) * page_size
        return success(
            {
                "records": rows[start : start + page_size],
                "pageNumber": page_num,
                "pageSize": page_size,
                "totalPage": (total_row + page_size - 1) // page_size,
                "totalRow": total_row,
            }
        )
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor list all sessions failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/sessions")
async def internal_list_monitor_sessions(
    page_num: int = Query(default=1, alias="pageNum"),
    page_size: int = Query(default=10, alias="pageSize"),
    status: str | None = None,
    app_id: str | None = Query(default=None, alias="appId"),
    user_id: str | None = Query(default=None, alias="userId"),
    session_id: str | None = Query(default=None, alias="sessionId"),
    trace_id: str | None = Query(default=None, alias="traceId"),
):
    query = MonitorSessionQueryRequest(
        pageNum=page_num,
        pageSize=page_size,
        status=status,
        appId=app_id,
        userId=user_id,
        sessionId=session_id,
        traceId=trace_id,
    )
    try:
        result = await get_monitor_query_service().list_sessions(query)
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor list sessions failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/sessions/{session_id}")
async def internal_get_monitor_session_detail(session_id: str):
    detail = await get_monitor_query_service().get_session_detail(session_id)
    if detail is None:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "session not found")
    return success(detail)


@monitor_router.get("/monitor/sessions/{session_id}/turns/{turn_id}")
async def internal_get_monitor_turn_detail(session_id: str, turn_id: str):
    detail = await get_monitor_query_service().get_turn_detail(session_id, turn_id)
    if detail is None:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "turn not found")
    return success(detail)


@monitor_router.get("/monitor/sessions/{session_id}/turns/{turn_id}/spans")
async def internal_get_monitor_turn_spans(session_id: str, turn_id: str):
    """turn 的 span 树（扁平列表 + offsetMs，前端组树画瀑布）。"""
    tree = await get_monitor_query_service().get_turn_spans(session_id, turn_id)
    if tree is None:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "turn not found")
    return success(tree)


@monitor_router.get("/monitor/config")
async def internal_get_monitor_config():
    try:
        result = await get_monitor_query_service().get_monitor_config()
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor config failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/alerts")
async def internal_list_monitor_alerts(
    page_num: int = Query(default=1, alias="pageNum"),
    page_size: int = Query(default=10, alias="pageSize"),
    status: str | None = None,
    level: str | None = None,
    rule_name: str | None = Query(default=None, alias="ruleName"),
    session_id: str | None = Query(default=None, alias="sessionId"),
):
    query = MonitorAlertQueryRequest(
        pageNum=page_num,
        pageSize=page_size,
        status=status,
        level=level,
        ruleName=rule_name,
        sessionId=session_id,
    )
    try:
        result = await get_monitor_query_service().list_alerts(query)
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor list alerts failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/tools/daily")
async def internal_get_tool_daily_stats(days: int = Query(default=7, ge=1, le=30)):
    """近 N 天工具执行统计（tool_semantics 按工具 × 自然日聚合，上表下图数据源）。"""
    try:
        result = await get_monitor_query_service().get_tool_daily_stats(days)
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor tool daily stats failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.get("/monitor/llm/daily")
async def internal_get_llm_daily_stats(days: int = Query(default=7, ge=1, le=30)):
    """近 N 天 LLM 调用统计（trace_spans 按模型 × 自然日聚合，上表下图数据源）。"""
    try:
        result = await get_monitor_query_service().get_llm_daily_stats(days)
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor llm daily stats failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.post("/monitor/cleanup")
async def internal_cleanup_monitor_history(
    retention_days: int = Query(default=7, alias="retentionDays"),
    dry_run: bool = Query(default=False, alias="dryRun"),
):
    try:
        result = await get_monitor_maintenance_service().cleanup_history(
            retention_days=retention_days, dry_run=dry_run
        )
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("monitor cleanup failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@monitor_router.post("/token-usage/query")
async def internal_query_token_usage(query: TokenUsageQueryRequest):
    try:
        result = await get_monitor_query_service().query_token_usage(query)
        return success(result)
    except BusinessException:
        raise
    except Exception as exc:
        log.exception("token usage query failed")
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


# ── Prometheus 指标（无 /api 前缀挂载） ──────────────────────

metrics_router = APIRouter(tags=["metrics"])


@metrics_router.get("/metrics", response_class=PlainTextResponse)
async def get_metrics():
    """Prometheus metrics endpoint (scraped by Prometheus — internal network only)."""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
