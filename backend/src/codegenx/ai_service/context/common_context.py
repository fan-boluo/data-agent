from abc import ABC
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from codegenx.ai_service.compact import microcompact_messages, estimate_tokens
from shared import log


@dataclass
class CommonContext(ABC):
    """智能体上下文基类：工作列表 + system prompt + 通用压缩。

    契约：chat_messages 只承载对话消息（不含 system）；
    LLM payload 由 assemble() 拼装（system 前置 + 标准化+tool 配对清理）。
    self._messages 已退役，agent 的全部读写都落在本对象上（leader=SessionContext 会话真相源，
    子智能体=SubContext 一次性工作区）。
    """

    session_id: str = ""
    app_id: str = ""
    user_id: str = ""
    # 关联数据库名（leader _prepare_prompt_params 需要）
    db_name: str = ""
    # agent 的 system prompt（_prepare_agent 构建后写入）
    system_prompt: str = ""
    chat_messages: list[dict[str, Any]] = field(default_factory=list)

    def init_chat_messages(self,user_msg:str) -> None:
        """
        turn 开始时初始化对话列表chat_messages
        Returns:
        """
        ...

    async def micro_compact(self):
        """通用的工具结果微压缩（就地替换 chat_messages）。"""
        log.debug("micro compact 前,{}", estimate_tokens(self.chat_messages))
        self.chat_messages = microcompact_messages(self.chat_messages)
        log.debug("micro compact 后,{}", estimate_tokens(self.chat_messages))

    async def compact(self, token_budget: int | None = None) -> AsyncGenerator[dict[str, Any], None]:
        """step 级压缩；token_budget 由统一预算器给出，未触发时不产出事件。"""
        ...

    def get_board(self, request_id: str = "") -> str:
        """当前请求看板渲染文本；子智能体无看板，缺省为空。"""
        return ""

    async def normalize_messages(self):
        self.chat_messages = self.drop_orphan_tool_pairs(self.normalize_history(self.chat_messages))

    async def assemble(self, payload_messages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """
        组装 LLM 调用消息（payload 组装的唯一入口）：system 前置 + 规范化 + tool 配对清理。
        payload_messages：预算器产出的消息视图（硬截断/占位剔除后）；缺省用真相源。
        仅构建新列表，不改真相源。
        """
        source = self.chat_messages if payload_messages is None else payload_messages
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self.drop_orphan_tool_pairs(self.normalize_history(source)))
        return messages

    @staticmethod
    def normalize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将对话标准化，完成后长这样：
        [
    {  "role": "system",      "content": "你是助手"    },
    {  "role": "user",        "content": "123"    },
    {  "role": "assistant",    "content": "我在",   "tool_calls": [{"id": "1"}]    },
    {  "role": "tool",        "content": "晴天",    "tool_call_id": "1",    "name": "get_weather"    }
]
        """
        normalized: list[dict[str, Any]] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue

            role = str(message.get("role", "")).strip()
            if role not in {"system", "user", "assistant", "tool"}:
                continue

            payload = {
                "role": role,
                "content": str(message.get("content", "") or ""),
            }
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                payload["tool_calls"] = message.get("tool_calls")
            if role == "assistant" and message.get("agent"):
                # 透传子 agent 回传标记（多 agent 链路附带字段，供历史视图过滤）
                payload["agent"] = str(message["agent"])
            if role == "tool":
                payload["tool_call_id"] = str(message.get("tool_call_id", "") or "")
                payload["name"] = str(message.get("name", "") or "")
            normalized.append(payload)
        return normalized

    @staticmethod
    def drop_orphan_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清理跨轮/压缩切点产生的不完整 tool 配对（仅作用于 payload 视图，不改真相源）。

        - tool 消息：前面无携带同 tool_call_id 的 assistant.tool_calls → 丢弃；
        - assistant.tool_calls：存在无对应 tool 响应的 id → 移除该 tool_call，
          全部移除后若 content 也为空 → 整条丢弃。
        """
        # 第一遍：收集每个 tool_call_id 是否有对应 tool 响应
        tool_call_ids: set[str] = set()
        tool_response_ids: set[str] = set()
        for m in messages:
            for tc in m.get("tool_calls") or []:
                tc_id = str(tc.get("id") or "")
                if tc_id:
                    tool_call_ids.add(tc_id)
            if m.get("role") == "tool":
                tid = str(m.get("tool_call_id") or "")
                if tid:
                    tool_response_ids.add(tid)

        cleaned: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "tool":
                # 孤儿 tool：无对应 assistant 调用
                if str(m.get("tool_call_id") or "") not in tool_call_ids:
                    continue
                cleaned.append(m)
                continue
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                # 切除没有 tool 响应的调用；全部缺失则按普通 assistant 处理
                kept_calls = [
                    tc for tc in tool_calls
                    if str(tc.get("id") or "") in tool_response_ids
                ]
                if len(kept_calls) != len(tool_calls):
                    m = dict(m)
                    if kept_calls:
                        m["tool_calls"] = kept_calls
                        cleaned.append(m)
                    elif m.get("content"):
                        m.pop("tool_calls", None)
                        cleaned.append(m)
                    continue
            cleaned.append(m)
        return cleaned