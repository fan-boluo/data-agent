"""平台资源与项目授权路由。

- /params/tool|skill/*：管理员维护平台资源（全局总开关 + 备注），skill-tool 关联只读展示
- /params/app/grant/*：App owner/admin 在平台已开放范围内开关本 App 的授权、覆写配置
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from codegenx.app_service.orm.app import App
from codegenx.app_service.services.access import require_manager
from codegenx.gateway.middleware.auth import require_login, require_role
from codegenx.gateway.middleware.jwt_auth import JWTUser
from codegenx.resource_service import agents, store
from codegenx.resource_service.schema import (
    AgentUpdateRequest,
    AppGrantListVO,
    AppGrantUpdateRequest,
    AppSkillGrantVO,
    AppToolGrantVO,
    PlatformAgentVO,
    PlatformSkillVO,
    PlatformToolVO,
    SkillUpdateRequest,
    ToolUpdateRequest,
)
from codegenx.user_service.user_enums import UserRole
from db.mysql.session import get_db_session
from shared import log
from shared.exceptions.error_code import ErrorCode
from shared.exceptions.throw_utils import ThrowUtils
from shared.schema.common import BaseResponse
from shared.utils.result_utils import success

router = APIRouter(prefix="/params", tags=["params"])


# ---------------------- 平台资源（管理员） ----------------------

@router.get("/tool/list", response_model=BaseResponse[list[PlatformToolVO]])
async def list_platform_tools(
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[list[PlatformToolVO]]:
    return success([PlatformToolVO(**item) for item in await store.list_tools()])


@router.post("/tool/update", response_model=BaseResponse[bool])
async def update_platform_tool(
    request: ToolUpdateRequest,
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[bool]:
    tool_code = request.tool_code.strip()
    ThrowUtils.throw_if(not tool_code, ErrorCode.PARAMS_ERROR, "工具编码不能为空")
    updated = await store.update_tool(
        tool_code, request.is_platform_enabled, request.remark, current_user.user_id
    )
    ThrowUtils.throw_if(not updated, ErrorCode.NOT_FOUND_ERROR, "工具不存在")
    log.info(
        "resource-service update tool code={} enabled={} operator={}",
        tool_code, request.is_platform_enabled, current_user.user_id,
    )
    return success(True)


@router.get("/skill/list", response_model=BaseResponse[list[PlatformSkillVO]])
async def list_platform_skills(
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[list[PlatformSkillVO]]:
    return success([PlatformSkillVO(**item) for item in await store.list_skills()])


@router.post("/skill/update", response_model=BaseResponse[bool])
async def update_platform_skill(
    request: SkillUpdateRequest,
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[bool]:
    skill_code = request.skill_code.strip()
    ThrowUtils.throw_if(not skill_code, ErrorCode.PARAMS_ERROR, "技能编码不能为空")
    updated = await store.update_skill(
        skill_code, request.is_platform_enabled, request.remark, current_user.user_id
    )
    ThrowUtils.throw_if(not updated, ErrorCode.NOT_FOUND_ERROR, "技能不存在")
    log.info(
        "resource-service update skill code={} enabled={} operator={}",
        skill_code, request.is_platform_enabled, current_user.user_id,
    )
    return success(True)


# ---------------------- 平台智能体（管理员，全局层） ----------------------

@router.get("/agent/list", response_model=BaseResponse[list[PlatformAgentVO]])
async def list_platform_agents(
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[list[PlatformAgentVO]]:
    # 每次进入页面实时重算：模型链全部熔断打开 → is_available=false（视为自动下线）
    return success([PlatformAgentVO(**item) for item in await agents.list_agents()])


@router.post("/agent/update", response_model=BaseResponse[bool])
async def update_platform_agent(
    request: AgentUpdateRequest,
    current_user: JWTUser = Depends(require_role(UserRole.ADMIN)),
) -> BaseResponse[bool]:
    agent_code = request.agent_code.strip()
    ThrowUtils.throw_if(not agent_code, ErrorCode.PARAMS_ERROR, "智能体编码不能为空")
    # 可用性拦截由前端做（不可用禁止开启）；这里只落手动开关与备注
    updated = await agents.update_agent(
        agent_code, request.is_enabled, request.remark, current_user.user_id
    )
    ThrowUtils.throw_if(not updated, ErrorCode.NOT_FOUND_ERROR, "智能体未注册")
    log.info(
        "resource-service update agent code={} enabled={} operator={}",
        agent_code, request.is_enabled, current_user.user_id,
    )
    return success(True)


# ---------------------- 项目授权（owner/admin） ----------------------

async def _require_app_manager(db: AsyncSession, app_id: str, login_user: JWTUser) -> App:
    """授权管理入口：项目必须存在，且仅 owner/admin 可操作。"""
    ThrowUtils.throw_if(not app_id, ErrorCode.PARAMS_ERROR, "应用 ID 错误")
    app = await db.get(App, app_id)
    ThrowUtils.throw_if(app is None, ErrorCode.NOT_FOUND_ERROR, "应用不存在")
    await require_manager(db, app, login_user)
    return app


@router.get("/app/grant/list", response_model=BaseResponse[AppGrantListVO])
async def list_app_grants(
    app_id: str = Query(alias="appId"),
    current_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
) -> BaseResponse[AppGrantListVO]:
    await _require_app_manager(db, app_id, current_user)
    tools = await store.list_app_tools(app_id)
    skills = await store.list_app_skills(app_id)
    return success(
        AppGrantListVO(
            tools=[AppToolGrantVO(**item) for item in tools],
            skills=[AppSkillGrantVO(**item) for item in skills],
        )
    )


@router.post("/app/grant/update", response_model=BaseResponse[bool])
async def update_app_grant(
    request: AppGrantUpdateRequest,
    current_user: JWTUser = Depends(require_login),
    db: AsyncSession = Depends(get_db_session),
) -> BaseResponse[bool]:
    await _require_app_manager(db, request.app_id, current_user)
    code = request.resource_code.strip()
    ThrowUtils.throw_if(not code, ErrorCode.PARAMS_ERROR, "资源编码不能为空")

    if request.resource_type == "tool":
        platform_enabled = await store.get_tool_platform_enabled(code)
        ThrowUtils.throw_if(platform_enabled is None, ErrorCode.NOT_FOUND_ERROR, "工具不存在")
        # 平台已下线的资源不允许在 App 侧重新开启（全局熔断优先）
        ThrowUtils.throw_if(
            request.is_enabled and not platform_enabled,
            ErrorCode.PARAMS_ERROR,
            "该工具已被平台下线，无法在当前项目启用",
        )
        await store.upsert_app_tool(
            request.app_id, code, request.is_enabled, request.override_config,
            request.remark, current_user.user_id,
        )
    else:
        platform_enabled = await store.get_skill_platform_enabled(code)
        ThrowUtils.throw_if(platform_enabled is None, ErrorCode.NOT_FOUND_ERROR, "技能不存在")
        ThrowUtils.throw_if(
            request.is_enabled and not platform_enabled,
            ErrorCode.PARAMS_ERROR,
            "该技能已被平台下线，无法在当前项目启用",
        )
        await store.upsert_app_skill(
            request.app_id, code, request.is_enabled, request.remark, current_user.user_id
        )

    log.info(
        "resource-service update app grant appId={} type={} code={} enabled={} operator={}",
        request.app_id, request.resource_type, code, request.is_enabled, current_user.user_id,
    )
    return success(True)
