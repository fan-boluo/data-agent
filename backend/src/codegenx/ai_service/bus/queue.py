"""Async message queue for decoupled channel-agent communication."""

import asyncio
from collections import defaultdict
from typing import Any

from codegenx.ai_service.agent.agent_schema import AgentEventType, AgentEvent, MessageType
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.agent.agent_schema import  AgentState
import json


class MessageBus(BaseComponent):
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """
    name = ComponentType.MESSAGE_BUS

    def __init__(self, system_app=None) -> None:
        BaseComponent.__init__(self, system_app)
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self._request_subscribers: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    async def publish_inbound(self, msg: Any) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> Any:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: Any) -> None:
        """Publish a response from the agent to request subscribers."""
        request_id = str(getattr(msg, "request_id", "") or "").strip()
        if not request_id:
            return

        subscribers = list(self._request_subscribers.get(request_id, ()))
        for queue in subscribers:
            await queue.put(msg)

    # async def consume_outbound(self) -> Any:
    #     """Consume the next outbound message (blocks until available)."""
    #     return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    # @property
    # def outbound_size(self) -> int:
    #     """Number of pending outbound messages."""
    #     return self.outbound.qsize()

    def subscribe_request(self, request_id: str) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._request_subscribers[str(request_id)].add(queue)
        return queue

    def unsubscribe_request(self, request_id: str, queue: asyncio.Queue[Any]) -> None:
        subscribers = self._request_subscribers.get(str(request_id))
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._request_subscribers.pop(str(request_id), None)

    def subscribe_turn(self, turn_id: str) -> asyncio.Queue[Any]:
        return self.subscribe_request(turn_id)

    def unsubscribe_turn(self, turn_id: str, queue: asyncio.Queue[Any]) -> None:
        self.unsubscribe_request(turn_id, queue)

    def _sanitize_event_data(self, event_type: AgentEventType, data: Any) -> None:
        """事件数据标准化（合并模式：原地修改 data dict，保留执行器携带的业务字段，追加标准 tool_name/tool_id/description）。

        TOOL_EXECUTION_START/END 两类事件的原始 data 来自 tool_executor.execute 发的：
        START 带 {name, id, arguments}；END 带 {name, tool_call_id, success, message, error_class, state}。
        这里把它们统一转成前端已消费的 tool_name/tool_id 命名，同时保留 error_class/success 等。
        """
        # data 可能是 None 或非 dict，兜底成空 dict 再继续（后续 publish_runtime_event 会再次兜底）
        if data is None:
            return
        if not isinstance(data, dict):
            return

        if event_type == AgentEventType.TOOL_EXECUTION_START:
            # name → tool_name，id → tool_id，arguments 保留（前端可能需要展示参数概要）
            data["tool_name"] = data.get("tool_name") or data.get("name", "")
            data["tool_id"] = data.get("tool_id") or data.get("id", "")
            if "description" not in data:
                data["description"] = f"执行工具: {data['tool_name']}"
            # 清理执行器原始字段
            for k in ("name", "id"):
                data.pop(k, None)

        elif event_type == AgentEventType.TOOL_EXECUTION_END:
            data["tool_name"] = data.get("tool_name") or data.get("name", "")
            data["tool_id"] = data.get("tool_call_id") or data.get("tool_id", "")
            # 优先用执行器传的 state（success/failed），否则从 success 字段推导
            if "state" not in data:
                data["state"] = "success" if data.get("success") else "failed"
            if "description" not in data:
                data["description"] = data.get("message") or data["tool_name"]
            # 清理执行器原始字段
            for k in ("name", "tool_call_id"):
                data.pop(k, None)
            # task_create/task_update 特殊处理：渲染 task_data 供前端任务面板消费
            if data["tool_name"] in ("task_create", "task_update"):
                render_raw = data.get("render", "") or data.get("description", "")
                if render_raw:
                    try:
                        task_data = json.loads(render_raw)
                        data["task_data"] = task_data
                        data["description"] = task_data.get("task", {}).get("subject", data["tool_name"])
                    except Exception:
                        data["description"] = render_raw

    async def publish_runtime_event(
        self,
        publish_context:dict,
        event_type: AgentEventType,
        state: AgentState,
        data=None,
        message_type=MessageType.TEXT
    ) -> None:
        # event结果整理后记录、发布
        if data is None:
            data = {}
        self._sanitize_event_data(event_type,data)
        await self.publish_outbound(
            AgentEvent(
                session_id=publish_context["session_id"],
                request_id=publish_context['request_id'],
                round_counter=publish_context['round_counter'],
                event_type=event_type,
                state=state,
                data=data,
                message_type=message_type
            )
        )

def initialize_message_bus(system_app) -> MessageBus:
    """注册聊天消息存储组件（system_app.initialize_components 调用）。"""
    return system_app.register(MessageBus)