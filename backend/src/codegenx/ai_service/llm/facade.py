"""LLMFacade —— llm/ 调用层的生命周期与观测门面（组件化）。

调用面保持 llm/ 模块函数（resilient_invoke / get_llm），本门面只负责：
  - async_before_stop：统一释放 provider 级共享连接池（close_llm_clients）；
  - async_after_start：全部组件预热完成后输出熔断器全量状态；
  - 观测聚合：全部熔断器状态快照（管理端点用）。

模型客户端预热不在门面做：使用模型的组件（AgentRegistry/BaseReActAgent/
CompactionService/SessionSummaryService/MemoryScheduler）在各自启动钩子中预热
自己需要的模型客户端与熔断器，随组件生命周期启停。
"""
from __future__ import annotations

from contextlib import suppress

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.system_app import SystemApp


class LLMFacade(BaseComponent):
    """LLM 调用层生命周期门面（不复制状态；业务代码继续直接调 llm/ 模块函数）。"""

    name = ComponentType.LLM_FACADE

    def init_app(self, system_app: SystemApp) -> None:
        self.system_app = system_app

    async def async_before_stop(self) -> None:
        # close_llm_clients 本身幂等（close 后清空注册表），残留调用无害
        with suppress(Exception):
            from codegenx.ai_service.llm.client_registry import close_llm_clients

            await close_llm_clients()

    async def async_after_start(self) -> None:
        """全部组件预热完成后输出熔断器全量状态（启动即确认预热覆盖率）。"""
        self.log_circuit_snapshot("startup")

    def circuit_snapshot(self) -> dict[str, str]:
        """ 熔断器快照 """
        from codegenx.ai_service.llm.resilience import circuit_snapshot

        return circuit_snapshot()

    def log_circuit_snapshot(self, prefix: str = "startup") -> None:
        """输出全部熔断器状态（启动完成/排障用）；观测失败不阻断主流程。"""
        from codegenx.ai_service.llm.resilience import log_breaker_snapshot

        with suppress(Exception):
            log_breaker_snapshot(prefix)


def initialize_llm(system_app: SystemApp) -> LLMFacade:
    """注册 LLM 生命周期门面组件（system_app.initialize_components 调用）。"""
    return system_app.register(LLMFacade)
