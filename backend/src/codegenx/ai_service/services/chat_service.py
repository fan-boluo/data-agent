from __future__ import annotations

import asyncio
from functools import lru_cache

from codegenx.ai_service.agent.auto_plan_manager import AutoPlanManager
from contextlib import suppress
from datetime import datetime
from typing import Any, AsyncGenerator
from codegenx.ai_service.agent.agent_schema import  AgentEvent,AgentState,AgentEventType
from codegenx.ai_service.agent.runtime_schema import (
    RuntimeSessionState, ActivateTurn,
)
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.hook import HookContext, HookEvent, hook_manager
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.trace import session_end as trace_session_end
from codegenx.ai_service.trace import session_start as trace_session_start
from shared import log
from codegenx.ai_service.schema.ai_schema import UserAIRequest


class ChatService(BaseComponent):
    name = ComponentType.CHAT_SERVICE
    """薄壳：生命周期与引擎访问全部委托 SystemApp 全局容器（docs/SystemApp架构设计.md §3.3）。"""
    def __init__(
        self,
        system_app=None,
    ):
        # 容器约定首参：register 以 component(system_app) 构造实例（BaseComponent 规范）
        BaseComponent.__init__(self, system_app)

        # 会话池为独立进程级组件（参数自读 config.session），此处仅取用
        self.session_pool = get_app().session_pool
        self.message_bus = get_app().message_bus

        # hook 监听器随组件模块 import 链完成 @on 收集，由 initialize_components() 末尾冻结（docs/Hook设计.md §6）
        self._dispatcher_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()


    def init_app(self, system_app) -> None:
        self.system_app = system_app

    # ------------------------------------------------------------------ lifecycle
    async def async_before_start(self) -> None:
        """
        应用启动前开启循环等待消息
        Returns:

        """
        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            return
        self._shutdown_event.clear()
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(), name="agent-runtime-dispatcher"
        )
        log.info("启动完成 dispatch_loop,等待消息。。。")

    async def async_before_stop(self) -> None:
        """引擎停止：dispatcher（session pool 由容器逆序广播自行收尾，失败不阻断其余组件）。"""
        with suppress(Exception):
            self._shutdown_event.set()
            if self._dispatcher_task is not None:
                self._dispatcher_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._dispatcher_task
                self._dispatcher_task = None

    async def stream_message(
            self, request: UserAIRequest
    ) -> AsyncGenerator[str, None]:
        """ 一次请求的消息收发 """
        await self.async_before_start()
        request_id = request.request_id
        # 针对改请求生成一个queue
        subscriber = self.message_bus.subscribe_request(request_id)
        try:
            await self.message_bus.publish_inbound(request)
            log.info("message_bus 输送请求：{}", request_id)
            while True:
                event = await subscriber.get()
                if not isinstance(event, AgentEvent):
                    continue
                log.info("stream message {}", event.model_dump_json())
                yield event.model_dump_json() + "\n"
                if event.event_type in {AgentEventType.REQUEST_COMPLETED, AgentEventType.REQUEST_STOPPED, AgentEventType.ERROR}:
                    await self._wait_for_request_cleanup(
                        str(request.session_id or ""), request_id
                    )
                    break
        finally:
            self.message_bus.unsubscribe_request(request_id, subscriber)


    async def stop_request(
            self,
            *,
            app_id: str = "",
            user_id: str = "",
            session_id: str,
            trace_id: str = "",
            request_id: str,
            reason: str = "user-stop",
    ) -> dict[str, Any]:
        """停止单个请求：活跃请求经 stop_signal 优雅停止，待处理请求从队列丢弃。

        会话状态变更委托 SessionPool.stop_request（见 session_pool.py），本方法仅负责
        为「未进入 runtime 的待处理请求」补发 REQUEST_STOPPED 事件——活跃请求的停止事件
        由 runtime 捕获 TurnStoppedError/CancelledError 时自行发布（runtime.py）。
        """
        result = await self.session_pool.stop_request(
            session_id, request_id, reason=reason
        )
        if result is None:
            log.info("stop_request 未命中活跃会话 sessionId={} requestId={}", session_id, request_id)
            return {
                "accepted": False,
                "sessionId": session_id,
                "stoppedRequestCount": 0,
                "droppedRequestCount": 0,
                "activeRequestIds": [],
                "droppedRequestIds": [],
                "activeTurnIds": [],
            }

        # 待处理请求从未进入 runtime，需在此补发 REQUEST_STOPPED；活跃请求由 runtime 自行发布
        for req in result["dropped_requests"]:
            await self.message_bus.publish_runtime_event(
                {
                    "session_id": str(getattr(req, "session_id", "") or "") or session_id,
                    "request_id": str(getattr(req, "request_id", "") or ""),
                    "round_counter": 0,
                },
                event_type=AgentEventType.REQUEST_STOPPED,
                state=AgentState.STOPPED.value,
                data={"reason": reason},
            )

        stopped_ids = result["stopped_request_ids"]
        log.info(
            "stop_request 完成 sessionId={} stopped={} dropped={} traceId={}",
            session_id, stopped_ids,
            [str(getattr(r, "request_id", "") or "") for r in result["dropped_requests"]], trace_id,
        )
        return {
            "accepted": bool(stopped_ids or result["dropped_requests"]),
            "sessionId": session_id,
            "stoppedRequestCount": len(stopped_ids),
            "droppedRequestCount": len(result["dropped_requests"]),
            "activeRequestIds": stopped_ids,
            "droppedRequestIds": [str(getattr(r, "request_id", "") or "") for r in result["dropped_requests"]],
            "activeTurnIds": result["active_step_ids"],
        }

    async def stop_session(
            self,
            *,
            session_id: str,
            reason: str = "user-stop",
    ) -> dict[str, Any]:
        """停止会话所有任务并关闭会话。

        会话状态变更委托 SessionPool.stop_session（设置 stop_signal、grace 等待活跃任务、
        关闭并移除会话）。多 agent 场景下，同一请求内嵌套 agent 共享 session 级 stop_signal，
        随活跃请求一并停止；活跃请求的停止事件由 runtime 自行发布，待处理请求由此处补发。
        """
        result = await self.session_pool.stop_session(
            session_id, reason=reason
        )
        if result is None:
            log.info("stop_session 未命中活跃会话 sessionId={}", session_id)
            return {
                "accepted": False,
                "sessionId": session_id,
                "stoppedRequestCount": 0,
                "droppedRequestCount": 0,
                "activeRequestIds": [],
                "droppedRequestIds": [],
                "activeTurnIds": [],
                "closed": False,
            }

        for req in result["dropped_requests"]:
            await self.message_bus.publish_runtime_event(
                {
                    "session_id": str(getattr(req, "session_id", "") or "") or session_id,
                    "request_id": str(getattr(req, "request_id", "") or ""),
                    "round_counter": 0,
                },
                event_type=AgentEventType.REQUEST_STOPPED,
                state=AgentState.STOPPED.value,
                data={"reason": reason},
            )

        # 会话已关闭：agent 间队列设施已移除，无需再清理 agent 收件箱残留
        stopped_ids = result["stopped_request_ids"]
        log.info(
            "stop_session 完成 sessionId={} stopped={} dropped={} closed={}",
            session_id, stopped_ids,
            [str(getattr(r, "request_id", "") or "") for r in result["dropped_requests"]],
            result["closed"],
        )
        return {
            "accepted": bool(stopped_ids or result["dropped_requests"]),
            "sessionId": session_id,
            "stoppedRequestCount": len(stopped_ids),
            "droppedRequestCount": len(result["dropped_requests"]),
            "activeRequestIds": stopped_ids,
            "droppedRequestIds": [str(getattr(r, "request_id", "") or "") for r in result["dropped_requests"]],
            "activeTurnIds": result["active_step_ids"],
            "closed": result["closed"],
        }


    # ------------------------------------------------------------------ dispatch loop

    async def _dispatch_loop(self) -> None:
        """消费 inbound requests 并传给 session workers."""
        while not self._shutdown_event.is_set():
            request = await self.message_bus.consume_inbound()
            log.info(
                "dispatcher loop 接收到请求：{} {}",
                request.request_id,
                str(getattr(request, "message", "") or "")[:10],
            )
            if not isinstance(request, UserAIRequest):
                continue

            # Get or create session using pool (no lock needed)
            session_state, is_new = await self.session_pool.get_or_create(request)
            # 会话起点须在 SESSION_START 钩子前就绪：trace 监听器以 started_at 计算会话时长
            session_state.state = AgentState.RUNNING
            session_state.started_at = datetime.utcnow()
            if is_new:
                # self.session_pool.init_session_state(session_state)
                # trace session 生命周期直调（观测直埋，不寄生 hook 通道）
                trace_session_start(session_state)
                log.debug(f"新建一个session_state:{request.session_id}")

            # P3 swap-out 恢复：闲置卸载过的会话按需从快照重载 chat_messages
            # （复用 on_session_start 已有的快照重载逻辑，docs/SystemApp架构设计.md §7）
            await self._restore_session_history(session_state)

            # 用户发送的消息入库
            user_dict = {"role": "user", "content": request.message}
            try:
                from codegenx.ai_service.chat_message import get_chat_message_store
                await get_chat_message_store().append_message(
                    request.user_id, str(request.app_id), request.session_id, user_dict
                )
            except Exception as exc:
                log.warning("user 消息入库失败（不影响对话）: {}", exc)

            # Add request to session pending queue and trigger processing
            await self._enqueue_session_request(session_state, request)
            log.debug("{} 已加入 session pending_requests", request.request_id)

    async def _restore_session_history(self, session_state: RuntimeSessionState) -> None:
        """每轮请求入口恢复会话历史：swap-out 复用与空闲回收后新建两种路径统一处理。

        内存 chat_messages 为空即按 turn 快照回填（新建会话同样适用：上一轮 session
        可能已被空闲回收、对象已从池中移除，仅剩快照文件）。
        """
        cm = session_state.context
        if cm is None or cm.chat_messages:
            return
        from codegenx.ai_service.system_app import get_app

        snapshot = await get_app().session_io.get_turn_chat_message_snapshot(
            user_id=session_state.user_id,
            app_id=session_state.app_id,
            session_id=session_state.session_id,
        ) or []
        if snapshot:
            cm.chat_messages = snapshot
            log.info("session {} 已从快照恢复（{} 条消息）",
                     session_state.session_id, len(cm.chat_messages))

    async def _wait_for_request_cleanup(self, session_id: str, request_id: str) -> None:
        """Wait for request cleanup using event notification (avoids busy-wait polling)."""
        session_state = await self.session_pool.get(session_id)
        if session_state is None:
            return
        if request_id not in session_state.active_tasks:
            return
        while True:
            try:
                await asyncio.wait_for(session_state.close_signal.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            # Re-read session_state in case pool replaced it
            session_state = await self.session_pool.get(session_id)
            if session_state is None or request_id not in session_state.active_tasks:
                return
            session_state.close_signal.clear()

    async def _enqueue_session_request(
            self, session_state: RuntimeSessionState, request: UserAIRequest
    ) -> None:
        """Add request to session pending list and trigger processing."""
        async with session_state.request_lock:
            session_state.pending_requests.append(request)
            session_state.touch()
            if session_state.processing:
                return
            session_state.processing = True
            session_state.worker_task = asyncio.create_task(
                self._process_session_requests(session_state),
                name=f"agent-session-{session_state.session_id}",
            )

    # ------------------------------------------------------------------ session worker

    async def _process_session_requests(self, session_state: RuntimeSessionState) -> None:
        """  携程"""
        session_state.state = AgentState.RUNNING
        # Ensure worker_task references the running task and mark processing
        session_state.worker_task = asyncio.current_task()
        session_state.processing = True
        try:
            while not self._shutdown_event.is_set() and not session_state.closed:
                async with session_state.request_lock:
                    # 没有等待的请求了，
                    if not session_state.pending_requests:
                        session_state.processing = False
                        return
                    request = session_state.pending_requests.pop(0)

                log.debug("session 中的请求任务触发, 处理请求: {}", request.request_id)

                session_state.request = request
                session_state.activate_turn = ActivateTurn()
                # 使用AutoPlan模式执行任务
                request_task = asyncio.create_task(
                    AutoPlanManager().execute_request(session_state),
                    name=f"agent-request-{session_state.request_id}",
                )
                log.debug("请求任务开始执行:{}", session_state.request_id)
                session_state.active_tasks[session_state.request_id] = request_task
                try:
                    await request_task
                except asyncio.CancelledError:
                    await self._close_session_state(session_state, end_reason="runtime-stop")
                    raise
                except Exception as exc:
                    log.opt(exception=True).error("Unhandled error in request {}: {}", session_state.request_id, exc)
                    await self.message_bus.publish_runtime_event(
                        session_state.publish_context, event_type=AgentEventType.ERROR, data=str(exc),
                        state=AgentState.FAILED,
                    )
                finally:
                    session_state.active_tasks.pop(session_state.request_id, None)
                    if not session_state.closed:
                        session_state.stop_signal.clear()
                        session_state.stop_reason = ""
                        session_state.close_signal.set()  # 通知 _wait_for_request_cleanup
                    session_state.touch()
                    log.info("{} finished",request.request_id)
        finally:
            async with session_state.request_lock:
                # Only clear processing/worker_task if this is the task that set them.
                current_task = asyncio.current_task()
                if session_state.worker_task is current_task:
                    session_state.processing = False
                    session_state.worker_task = None
                log.info("{},worker 空闲退出（会话与历史保留在池中）", session_state.session_id)

    async def _close_session_state(
            self, session_state: RuntimeSessionState, *, end_reason: str
    ) -> None:
        """Close session and cleanup resources (pool handles removal)."""
        if session_state.closed:
            return

        session_state.closed = True
        # 单 agent 链路退役：active_steps 已删除，runtime-stop 直接判定为 STOPPED
        if end_reason == "runtime-stop":
            session_state.state = AgentState.STOPPED
        elif session_state.state != AgentState.FAILED:
            session_state.state = AgentState.COMPLETED

        if session_state.worker_task is not None and session_state.worker_task.cancelled():
            session_state.worker_task = None

        await hook_manager.emit(
            HookEvent.SESSION_END,
            HookContext(
                event=HookEvent.SESSION_END,
                session=session_state,
                data={"end_reason": end_reason},
            ),
        )
        # trace session 收口直调（emit 后：等记忆监听器完成再兜底 flush，等价原 priority=90）
        await trace_session_end(session_state)

        # Pool will automatically remove closed sessions during cleanup
        log.info("Session {} closed: end_reason={}", session_state.session_id, end_reason)


# ------------------------------------------------------------------ monitoring

    async def get_runtime_stats(self) -> dict[str, Any]:
        """Get runtime statistics for monitoring and debugging."""
        pool_stats = self.session_pool.stats()
        return {
            "session_pool": pool_stats,
            "dispatcher_active": self._dispatcher_task is not None
            and not self._dispatcher_task.done(),
        }

def initialize_chat_service(system_app) -> ChatService:
    """注册全局对话引擎组件（system_app.initialize_components 调用；
    实际启动/停止在 async_before_start / async_before_stop 钩子中）。"""
    return system_app.register(ChatService)


