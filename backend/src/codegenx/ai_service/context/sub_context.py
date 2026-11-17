from codegenx.ai_service.context.common_context import CommonContext
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

@dataclass
class SubContext(CommonContext):
    """专属于子智能体的上下文（一次性工作区，随 run 生灭）。

    与 SessionContext 的差异：无会话摘要水位、无快照/恢复;
    compact_after_step 级压缩直接走基类默认实现（Path B LLM 摘要）；
    leader 的会话级 Path A/水位逻辑对它无意义也不允许触达。

    agent_name 为子智能体专属字段（不再放在 CommonContext 基类）：
    leader 的归属在 RuntimeSessionState.agent_name，子智能体的归属在 SubContext 自身。
    """

    agent_name: str = ""

    def init_chat_messages(self,user_msg:str) -> None:
        """初始化本轮对话工作列表：仅清空。

        任务文本由 base_react_agent.run() 统一追加（agent_message.task）。
        此处不能再 append：_prepare_agent 传入的是 session_state.request.message
        （用户原始请求），追加会把与子任务无关的用户消息泄漏进子智能体工作区。
        """
        self.chat_messages.clear()

    async def compact(self, token_budget: int | None = None) -> AsyncGenerator[dict[str, Any], None]:
        """step 级压缩（默认 Path B：LLM 摘要，无会话水位）；未触发时不产出事件。"""
        from codegenx.ai_service.system_app import get_app

        compacted, result = await get_app().compaction.compact_if_needed(
            self.chat_messages, threshold=token_budget,
        )
        self.chat_messages = compacted
        if result is not None:
            yield {
                "path_used": result.path_used,
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "messages_removed": result.messages_removed,
            }


