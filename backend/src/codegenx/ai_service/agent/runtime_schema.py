from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from codegenx.ai_service.agent.agent_schema import AgentState
from codegenx.ai_service.context.session_context import SessionContext
from codegenx.ai_service.schema.ai_schema import UserAIRequest


class TurnStoppedError(Exception):
    pass

@dataclass
class ActivateTurn:
    """turn 级请求执行状态机（基础设施层，单/多 agent 链路共用）。

    单 agent 链路（AgentRuntime）已退役；span 引用（active_span_refs）随
    monitor 双轨采集一起删除，链路追踪统一走 telemetry（trace 模块）。
    """
    round_counter: int = 0  # 跨 agent 路由轮次（多 agent 链路的循环边界）
    state: AgentState = AgentState.IDLE  # 一次turn的状态
    started_at: float = 0.0
    finished_at: float = 0.0
    error_text: str = ""


def raise_if_stop_signal(session_state: RuntimeSessionState) -> None:
    if session_state.stop_signal.is_set():
        raise TurnStoppedError(session_state.stop_reason or "user-stop")


@dataclass
class RuntimeSessionState:
    """Persistent session state. Per-request fields are reset by _reset_request_state.

    P2 瘦身：runtime/session_manager/task_manager 成员对象已删除——
    引擎是全局组件（get_app().runtime），落盘与任务看板是无状态服务
    （get_app().session_io / get_app().tasks），ids 一律作调用参数。
    """
    session_id: str
    request: UserAIRequest
    context_manager: SessionContext | None = None  # 整个会话的上下文管理（由 on_session_start hook 初始化）
    # P4 §10.3：会话归属智能体（请求 metadata.agent_name；空=默认智能体，现行为不变）
    agent_name: str = ""

    # session 中的请求任务
    request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 获取请求任务锁
    pending_requests: list[UserAIRequest] = field(default_factory=list)  # 等待的请求
    active_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)  # 活跃的请求任务
    activate_turn:ActivateTurn = field(default_factory=ActivateTurn)  # 当前获取的turn

    # Session lifecycle
    started_at: datetime | None = None
    last_activity_at: float = 0.0
    state: AgentState = AgentState.IDLE
    processing: bool = False
    closed: bool = False
    close_signal: asyncio.Event = field(default_factory=asyncio.Event)  # 用于 event 通知替代忙等轮询
    stop_signal: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: str = ""
    worker_task: asyncio.Task | None = None  # 一个携程任务

    # 说明：Supervisor 编排共享状态已收敛至任务看板（按 request 落盘），
    # RuntimeSessionState 保持纯运行时调度职责。


    def touch(self) -> None:
        self.last_activity_at = time.time()

    @property
    def trace_id(self) -> str:
        return str(getattr(self.request, "trace_id", "") or "")

    @property
    def app_id(self) -> str:
        return str(getattr(self.request, "app_id", "main") or "main")

    @property
    def db_name(self) -> str | None:
        return getattr(self.request, "db_name", None)

    @property
    def available_schemas(self) -> list[str]:
        """当前会话可问答的公共数据源白名单（格式 datasource.schema）。"""
        return list(getattr(self.request, "available_schemas", None) or [])

    @property
    def current_db(self) -> str | None:
        """用户当前选择的数据集（datasource.schema）；未选择时为 None。"""
        return getattr(self.request, "current_db", None)

    @property
    def user_id(self) -> str:
        return str(getattr(self.request, "user_id", "") or "")

    @property
    def request_id(self) -> str:
        return str(getattr(self.request, "request_id", "") or "")

    @property
    def client_version(self) -> str:
        return str(getattr(self.request, "client_version", "ai-service") or "ai-service")

    @property
    def audit_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "request_id": self.request.request_id,
        }

    @property
    def publish_context(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request_id": self.request.request_id,
            "round_counter": self.activate_turn.round_counter
        }

