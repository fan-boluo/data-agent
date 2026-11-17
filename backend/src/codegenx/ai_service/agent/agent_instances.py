"""智能体配置解析与规格注册（组合根调用）。

各智能体在此模块通过类方法完成两步：
1. resolve_agent_config() — 按 cls.name 从 config.json 匹配配置，存为类属性
2. register() — 将 id/role/name/desc/cls 构建 AgentSpec 注册到 AgentRegistry

新增智能体只需：1) 实现 Agent 子类 2) 在 _AGENT_CLASSES 列表中添加即可。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from shared import log

if TYPE_CHECKING:
    from codegenx.ai_service.system_app import SystemApp


# ── 智能体类清单（新增智能体在此添加）──────────────────────────────────────
_AGENT_CLASSES: list[type] = []  # 延迟填充，见下方 _get_agent_classes


def _get_agent_classes() -> list[type]:
    """延迟 import 避免模块级循环依赖。"""
    if _AGENT_CLASSES:
        return _AGENT_CLASSES
    from codegenx.ai_service.agent.agents.leader_agent import LeaderAgent
    from codegenx.ai_service.agent.agents.plan_agent import PlannerAgent
    from codegenx.ai_service.agent.agents.data_explore_agent import DateExploreAgent
    from codegenx.ai_service.agent.agents.csv_explore_agent import CsvExploreAgent
    from codegenx.ai_service.agent.agents.report_agent import ReportAgent

    _AGENT_CLASSES.extend([
        LeaderAgent,
        PlannerAgent,
        DateExploreAgent,
        CsvExploreAgent,
        ReportAgent,
    ])
    return _AGENT_CLASSES


def initialize_agent_instances(app: "SystemApp") -> None:
    """解析各智能体配置并注册规格到 AgentRegistry。

    由 system_app.initialize_components 在所有组件注册完毕后调用：
    1. cls.resolve_agent_config() — 读取 config.json，存为类属性
    2. cls.register() — 构建 AgentSpec 并注册到 AgentRegistry
    """
    for agent_cls in _get_agent_classes():
        agent_cls.resolve_agent_config()
        agent_cls.register()
    log.info("智能体配置解析与注册完成：{} 个", len(app.agents.all_specs()))
