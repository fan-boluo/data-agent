"""平台资源（tool/skill）与项目授权接口模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from shared.schema.common import CamelBaseModel


# ---------------------- 平台资源 ----------------------

class PlatformToolVO(CamelBaseModel):
    tool_code: str = Field(alias="toolCode")
    tool_name: str | None = Field(default=None, alias="toolName")
    description: str | None = None
    is_platform_enabled: bool = Field(default=True, alias="isPlatformEnabled")
    remark: str | None = None
    operator_id: str | None = Field(default=None, alias="operatorId")
    skill_codes: list[str] = Field(default_factory=list, alias="skillCodes")
    update_time: datetime | None = Field(default=None, alias="updateTime")


class PlatformSkillVO(CamelBaseModel):
    skill_code: str = Field(alias="skillCode")
    skill_name: str | None = Field(default=None, alias="skillName")
    description: str | None = None
    is_platform_enabled: bool = Field(default=True, alias="isPlatformEnabled")
    remark: str | None = None
    operator_id: str | None = Field(default=None, alias="operatorId")
    tool_codes: list[str] = Field(default_factory=list, alias="toolCodes")
    update_time: datetime | None = Field(default=None, alias="updateTime")


class ToolUpdateRequest(CamelBaseModel):
    tool_code: str = Field(alias="toolCode")
    is_platform_enabled: bool = Field(alias="isPlatformEnabled")
    remark: str | None = None


class SkillUpdateRequest(CamelBaseModel):
    skill_code: str = Field(alias="skillCode")
    is_platform_enabled: bool = Field(alias="isPlatformEnabled")
    remark: str | None = None


# ---------------------- 平台智能体（全局层，无 app 级授权） ----------------------

class AgentModelStateVO(CamelBaseModel):
    """单个模型的熔断状态（available=False 表示熔断打开且冷却期内）。"""
    model: str
    state: str
    available: bool


class PlatformAgentVO(CamelBaseModel):
    agent_code: str = Field(alias="agentCode")
    agent_name: str | None = Field(default=None, alias="agentName")
    role: str | None = None
    description: str | None = None
    is_enabled: bool = Field(default=True, alias="isEnabled")       # 手动总开关（DB）
    is_available: bool = Field(default=True, alias="isAvailable")   # LLM 可用性（实时计算）
    unavailable_reason: str | None = Field(default=None, alias="unavailableReason")
    models: list[AgentModelStateVO] = Field(default_factory=list)
    remark: str | None = None
    operator_id: str | None = Field(default=None, alias="operatorId")
    update_time: datetime | None = Field(default=None, alias="updateTime")


class AgentUpdateRequest(CamelBaseModel):
    agent_code: str = Field(alias="agentCode")
    is_enabled: bool = Field(alias="isEnabled")
    remark: str | None = None


# ---------------------- 项目授权 ----------------------

class AppToolGrantVO(CamelBaseModel):
    tool_code: str = Field(alias="toolCode")
    tool_name: str | None = Field(default=None, alias="toolName")
    description: str | None = None
    is_platform_enabled: bool = Field(default=True, alias="isPlatformEnabled")
    is_enabled: bool = Field(default=False, alias="isEnabled")
    default_config: dict = Field(default_factory=dict, alias="defaultConfig")   # 平台工具原始配置（覆写弹窗初值）
    override_config: dict = Field(default_factory=dict, alias="overrideConfig")
    remark: str | None = None
    operator_id: str | None = Field(default=None, alias="operatorId")
    update_time: datetime | None = Field(default=None, alias="updateTime")


class AppSkillGrantVO(CamelBaseModel):
    skill_code: str = Field(alias="skillCode")
    skill_name: str | None = Field(default=None, alias="skillName")
    description: str | None = None
    is_platform_enabled: bool = Field(default=True, alias="isPlatformEnabled")
    is_enabled: bool = Field(default=False, alias="isEnabled")
    remark: str | None = None
    operator_id: str | None = Field(default=None, alias="operatorId")
    update_time: datetime | None = Field(default=None, alias="updateTime")


class AppGrantListVO(CamelBaseModel):
    tools: list[AppToolGrantVO] = Field(default_factory=list)
    skills: list[AppSkillGrantVO] = Field(default_factory=list)


class AppGrantUpdateRequest(CamelBaseModel):
    app_id: str = Field(alias="appId")
    resource_type: Literal["tool", "skill"] = Field(alias="resourceType")
    resource_code: str = Field(alias="resourceCode")
    is_enabled: bool = Field(alias="isEnabled")
    override_config: dict | None = Field(default=None, alias="overrideConfig")
    remark: str | None = None
