"""AI 服务路由：Agent 对话流 / 会话历史 / 监控 / Prometheus 指标。

由原 ai-service/app.py 提取（去 sys.path 注入 / importlib 加载 / Nacos 注册）。
- chat_router  → /api/ai/*（原公开端点，现统一走网关认证）
- monitor_router → /api/stats/admin/*（替代原 routes.yaml 对 /internal 的重写转发，收紧为管理员）
- metrics_router → /metrics（无前缀，供 Prometheus 抓取）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from codegenx.ai_service.hook import hook_manager, HookEvent, HookContext, HookAction
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.chat_message import get_chat_message_store
from codegenx.ai_service.trace.maintenance import get_monitor_maintenance_service
from codegenx.ai_service.trace.query_service import get_monitor_query_service
from codegenx.app_service.services.access import require_participant_by_id
from codegenx.gateway.middleware.auth import require_login, require_role
from codegenx.gateway.middleware.jwt_auth import JWTUser
from db.mysql.session import get_db_session
from shared import log
from codegenx.user_service.user_enums import UserRole
from shared.exceptions.business_exception import BusinessException
from shared.exceptions.error_code import ErrorCode
from codegenx.ai_service.schema.ai_schema import (
    UserAIRequest,
    AiServiceStopRequest,
    AiServiceStopResponse,
)
from shared.constants import PUBLIC_DATASETS, PUBLIC_SCHEMAS
from codegenx.ai_service.trace.schema.admin import (
    MonitorAlertQueryRequest,
    MonitorSessionQueryRequest,
    TokenUsageQueryRequest,
)
from shared.utils.result_utils import success



# ── 对话 / 会话历史 ──────────────────────────────────────────

chat_router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_login)])


class SessionListItem(BaseModel):
    session_id: str
    first_message: str
    create_time: str


@chat_router.get("/datasets")
async def list_public_datasets(
    login_user: JWTUser = Depends(require_login),
):
    """可选数据集列表（前端输入框选择器用；后续权限管控后按用户圈定）。"""
    return success(data=list(PUBLIC_DATASETS))


@chat_router.post("/chat/gen")
async def generate_code_stream(
    request: UserAIRequest,
    login_user: JWTUser = Depends(require_login),
):
    # 身份一律取 JWT 登录态，请求体 userId 不可信（防会话/工作区挂错用户）
    request.user_id = str(login_user.user_id)
    # 可用公共数据源以后端为准（覆盖前端传值，防伪造圈定任意库；后续权限管控后按用户圈定）
    request.available_schemas = list(PUBLIC_SCHEMAS)
    # 当前数据集必须落在白名单内，越界或非法值一律置空（由 agent 反问用户选择）
    if request.current_db and request.current_db not in PUBLIC_SCHEMAS:
        log.warning("忽略非法 currentDb: {}", request.current_db)
        request.current_db = None
    trace_id, request_id, session_id = _validate_call_context(request)
    ctx = HookContext(event=HookEvent.ON_USER_INPUT,data={"user_input":request.message})
    res = await hook_manager.emit(HookEvent.ON_USER_INPUT,ctx)
    if res.action == HookAction.BLOCKED:
        raise BusinessException(ErrorCode.FORBIDDEN_ERROR, str(res.message))
    log.info(
        "ai-service public stream request traceId={} requestId={} appId={} messageLen={} preview={}",
        trace_id,
        request_id,
        request.app_id,
        len(request.message),
        request.message[:80],
    )
    log.info("ai-service public stream request {} ", request.model_dump_json())
    try:
        stream = get_app().chat_service.stream_message(request)
        async def event_stream():
            async for chunk in stream:
                yield chunk

        return StreamingResponse(event_stream(), media_type="text/plain")
    except Exception as exc:
        log.exception(
            "ai-service public stream failed traceId={} requestId={} appId={} error:{}",
            trace_id,
            request_id,
            request.app_id,
            str(exc),
        )
        raise


@chat_router.post("/chat/stop")
async def stop_code_stream(
    request: AiServiceStopRequest,
    login_user: JWTUser = Depends(require_login),
):
    trace_id, request_id, session_id = _validate_stop_context(request)
    # 同 chat/gen：停止请求身份也以 JWT 登录态为准
    user_id = str(login_user.user_id)
    log.info(
        "ai-service public stop request traceId={} requestId={} appId={} sessionId={} reason={}",
        trace_id,
        request_id,
        request.app_id,
        session_id,
        request.reason,
    )
    try:
        result = await get_app().chat_service.stop_request(
            app_id=request.app_id,
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            request_id=request_id,
            reason=request.reason,
        )
        return success(AiServiceStopResponse.model_validate(result).model_dump(by_alias=True))
    except BusinessException:
        raise
    except Exception as exc:
        log.exception(
            "ai-service public stop failed traceId={} requestId={} appId={} sessionId={}",
            trace_id,
            request_id,
            request.app_id,
            session_id,
        )
        raise BusinessException(ErrorCode.SYSTEM_ERROR, str(exc)) from exc


@chat_router.get("/sessions/{app_id}")
async def list_sessions(
    app_id: str,
    limit: int = Query(default=5, ge=1, le=20),
    login_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
):
    """列出「当前用户 + app」下最近的 session（会话按用户隔离，chat_message 表聚合）。"""
    await require_participant_by_id(db, app_id, login_user)
    entries = await get_chat_message_store().list_sessions(
        user_id=str(login_user.user_id), app_id=str(app_id), limit=limit
    )
    return success([SessionListItem(**e) for e in entries])


@chat_router.get("/sessions/{app_id}/{session_id}/messages")
async def get_session_messages(
    app_id: str,
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    login_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
):
    """加载指定 session 的最近 N 条消息（仅限项目成员，按用户隔离查询）。"""
    await require_participant_by_id(db, app_id, login_user)
    try:
        messages = await get_chat_message_store().get_recent(
            user_id=login_user.user_id, session_id=session_id, limit=limit
        )
    except Exception as exc:
        log.warning("读取会话消息失败 session={}/{} err={}", app_id, session_id, exc)
        raise BusinessException(ErrorCode.SYSTEM_ERROR, "读取会话消息失败") from exc

    return success(messages)


@chat_router.get("/sessions/{app_id}/{session_id}/alive")
async def check_session_alive(
    app_id: str,
    session_id: str,
    login_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
):
    """检查 session 在内存池中是否活跃（仅限项目成员）。"""
    await require_participant_by_id(db, app_id, login_user)
    # 容器未启动（lifespan 未跑完）时与 stop_session 一致：视为不活跃而非系统错误
    try:
        session_pool = get_app().session_pool
    except RuntimeError:
        session_pool = None
    alive = bool(session_pool is not None and await session_pool.exists(session_id))
    return success({"alive": alive})


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


# ── 请求上下文校验 ──────────────────────────────────────────

def _validate_call_context(request: UserAIRequest) -> tuple[str, str, str]:
    trace_id = str(request.trace_id or "").strip()
    request_id = str(request.request_id or "").strip()
    session_id = str(request.session_id or "").strip()
    if not trace_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "traceId 不能为空")
    if not request_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "requestId 不能为空")
    if not session_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "sessionId 不能为空")

    return trace_id, request_id, session_id


def _validate_stop_context(request: AiServiceStopRequest) -> tuple[str, str, str]:
    trace_id = str(request.trace_id or "").strip()
    request_id = str(request.request_id or "").strip()
    session_id = str(request.session_id or "").strip()
    if not trace_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "traceId 不能为空")
    if not request_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "requestId 不能为空")
    if not session_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "sessionId 不能为空")
    return trace_id, request_id, session_id
