from __future__ import annotations

from typing import Any

from pydantic import Field

from shared.schema.common import CamelBaseModel


class UserAIRequest(CamelBaseModel):
    app_id: str = Field(alias="appId")
    # 身份一律由路由层从 JWT 登录态覆写（历史默认值 "userx" 已删，防止会话/工作区挂错用户）
    user_id: str = Field(default="", alias="userId")
    message: str
    session_id: str = Field(default="", alias="sessionId")
    trace_id: str  = Field(default="", alias="traceId")
    request_id: str = Field(default="", alias="requestId")
    client_version: str = Field(default="ai-service", alias="clientVersion")
    db_name: str | None = Field(default=None, alias="dbName")
    # 当前用户可问答的公共数据源列表（权限白名单，由路由层注入）
    available_schemas: list[str] = Field(default_factory=list, alias="availableSchemas")
    # 用户当前选择的数据集（格式 datasource.schema，如 business.dvdrental），agent 只聚焦此库
    current_db: str | None = Field(default=None, alias="currentDb")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AiServiceStopRequest(CamelBaseModel):
    app_id: str = Field(alias="appId")
    user_id: str | None = Field(default=None, alias="userId")
    session_id: str = Field(alias="sessionId")
    trace_id: str | None = Field(default=None, alias="traceId")
    request_id: str | None = Field(default=None, alias="requestId")
    reason: str | None = None


class AiServiceStopResponse(CamelBaseModel):
    accepted: bool
    session_id: str = Field(alias="sessionId")
    stopped_request_count: int = Field(alias="stoppedRequestCount")
    dropped_request_count: int = Field(alias="droppedRequestCount")
    active_request_ids: list[str] = Field(default_factory=list, alias="activeRequestIds")
    dropped_request_ids: list[str] = Field(default_factory=list, alias="droppedRequestIds")
    active_step_ids: list[str] = Field(default_factory=list, alias="activeTurnIds")
