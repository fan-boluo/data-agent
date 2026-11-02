"""网关聊天路由 — SSE 流 + 限流 + userId 注入 + 项目成员校验 + 项目库解析。

进程内直调 ai_service 的流式处理器，不再经 HTTP 代理转发
（顺带修复了原代理指向不存在路径 /api/ai/codegen/stream 的问题）。

权限：gen/stop 均要求当前登录用户是 appId 项目的 owner/member/admin；
dbName 以 app 记录为准（后端覆盖前端传值，防伪造 dbName 查任意库）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from codegenx.ai_service.api.router import generate_code_stream, stop_code_stream
from codegenx.app_service.services.access import require_participant_by_id
from codegenx.gateway.middleware.auth import require_login
from codegenx.gateway.middleware.jwt_auth import JWTUser
from codegenx.gateway.services.rate_limit_service import RateLimitService
from db.mysql.session import get_db_session
from db.redis.redis_client import get_redis_client
from codegenx.ai_service.schema.ai_schema import UserAIRequest, AiServiceStopRequest
from shared.constants import PUBLIC_SCHEMAS

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/gen/code")
async def chat_to_gen_code_post(
    payload: dict[str, Any],
    login_user: JWTUser = Depends(require_login),
    redis: "Redis" = Depends(get_redis_client),  # type: ignore[type-arg]
    db: AsyncSession = Depends(get_db_session),
):
    await RateLimitService(redis).check_user_rate_limit(
        login_user.user_id, "gen_code", 5
    )

    # ID 迁移后 appId 为 app_xxxx 字符串，直接透传（空值校验收口在 access 层）
    app_id = str(payload.get("appId") or "").strip()
    # 登录用户必须是项目 owner/member/admin
    app = await require_participant_by_id(db, app_id, login_user)

    if "userId" not in payload:
        payload["userId"] = str(login_user.user_id)

    # dbName 后端以项目记录为准，覆盖前端传值（前端传值仅参考，防伪造查任意库）
    payload["dbName"] = app.db_name
    # 可用公共数据源：权限白名单（agent 只聚焦 currentDb，用户自己的库不在问答范围）
    payload["availableSchemas"] = list(PUBLIC_SCHEMAS)
    # 当前数据集必须落在白名单内，越界或非法值一律置空（由 agent 反问用户选择）
    if payload.get("currentDb") and payload["currentDb"] not in PUBLIC_SCHEMAS:
        payload["currentDb"] = None

    request = UserAIRequest.model_validate(payload)
    return await generate_code_stream(request)


@router.post("/stop")
async def chat_to_gen_code_stop(
    payload: dict[str, Any],
    login_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
):
    # ID 迁移后 appId 为 app_xxxx 字符串，直接透传（空值校验收口在 access 层）
    app_id = str(payload.get("appId") or "").strip()
    await require_participant_by_id(db, app_id, login_user)

    if "userId" not in payload:
        payload["userId"] = str(login_user.user_id)

    request = AiServiceStopRequest.model_validate(payload)
    return await stop_code_stream(request)
