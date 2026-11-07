"""Session resource pool with automatic cleanup and LRU management."""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Optional
from codegenx.ai_service.context.session_context import SessionContext
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.utils.config import config
from shared import log


class SessionPool(BaseComponent):
    """
    Manages session lifecycle with intelligent cleanup strategies.

    Features:
    - LRU-based session eviction
    - Configurable idle timeout
    - Graceful session closure
    - Memory-efficient session management
    """

    name = ComponentType.SESSION_POOL

    def __init__(
        self,
        system_app=None,
    ):
        """会话池组件：参数自读 config.session（SessionConfig），与智能体配置解耦；
        启动/关闭由容器生命周期广播接管（async_before_start / async_before_stop）。

        Args:
            system_app: 容器引用（system_app.register 以 component(system_app) 构造）
        """
        BaseComponent.__init__(self, system_app)
        session_cfg = config.session
        self.max_sessions = int(session_cfg.max_sessions or 100)
        self.idle_timeout_seconds = int(session_cfg.idle_timeout_seconds or 1800)
        self.cleanup_interval_seconds = int(session_cfg.cleanup_interval_seconds or 300)
        self.swap_idle_seconds = int(session_cfg.swap_idle_seconds or 300)
        self.grace_stop_seconds = int(session_cfg.grace_stop_seconds or 2.0)

        # OrderedDict maintains insertion order for LRU tracking
        self._sessions: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    async def start(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._shutdown_event.clear()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="session-pool-cleanup",
        )
        log.info(
            "SessionPool started: max_sessions={}, idle_timeout={}s",
            self.max_sessions,
            self.idle_timeout_seconds,
        )

    async def stop(self) -> None:
        """Stop cleanup task and close all sessions."""
        self._shutdown_event.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        async with self._lock:
            session_ids = list(self._sessions.keys())
            for session_id in session_ids:
                await self._close_session_unsafe(session_id)
            self._sessions.clear()
        log.info("SessionPool stopped")

    # ── 容器生命周期钩子：start_app/stop_app 广播接管 ──

    async def async_before_start(self) -> None:
        await self.start()

    async def async_before_stop(self) -> None:
        await self.stop()

    async def get_or_create(
        self,  request: Any
    ) -> tuple[Any, bool]:
        """
        Get existing session or create new one.

        Args:
            session_id: Unique session identifier
            request: AiServiceGenerateRequest

        Returns:
            Tuple of (session_state, is_new_session)
        """
        from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState

        session_id = str(request.session_id or "")
        async with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                self._sessions.move_to_end(session_id)
                # 注意：此处不得改写 session.request——执行中的请求动态读取
                # request_id/publish_context/db_name，入队新请求时改写会导致
                # 看板写入他人 request 文件、SSE 事件发错订阅队列（竞态）。
                # request 由 worker pop 时统一设置（chat_service）。
                session.touch()
                return session, False

            # Check if we need to evict LRU session
            if len(self._sessions) >= self.max_sessions:
                lru_session_id = next(iter(self._sessions))
                await self._close_session_unsafe(lru_session_id)
                del self._sessions[lru_session_id]
                log.warning(
                    "SessionPool reached max capacity, evicted LRU session: {}",
                    lru_session_id,
                )

            session = RuntimeSessionState(
                session_id=session_id,
                request=request,
            )
            session.context_manager = SessionContext(
                session_id=session.session_id,
                app_id=session.app_id,
                user_id=session.user_id,
                db_name=session.db_name,
                agent_name=session.agent_name,
            )

            # 上次聊天消息到chat_message??

            # P4 §10.3：会话归属智能体（请求 metadata.agent_name；空=默认智能体）
            session.agent_name = str((getattr(request, "metadata", None) or {}).get("agent_name", "") or "")

            session.touch()
            self._sessions[session_id] = session
            return session, True

    async def exists(self, session_id: str) -> bool:
        """Check if session exists and is not closed."""
        async with self._lock:
            if session_id not in self._sessions:
                return False
            session = self._sessions[session_id]
            return not getattr(session, "closed", False)

    async def get(self, session_id: str) -> Optional[Any]:
        """Get session if it exists and is not closed."""
        async with self._lock:
            if session_id not in self._sessions:
                return None
            session = self._sessions[session_id]
            if getattr(session, "closed", False):
                del self._sessions[session_id]
                return None
            return session

    async def remove(self, session_id: str) -> None:
        """Remove and close session."""
        async with self._lock:
            await self._close_session_unsafe(session_id)
            self._sessions.pop(session_id, None)

    async def stop_request(
        self,
        session_id: str,
        request_id: str,
        *,
        reason: str = "user-stop",
    ) -> dict[str, Any] | None:
        """停止单个请求：活跃任务经 stop_signal 触发 TurnStoppedError，待处理请求从队列丢弃。

        仅变更会话状态，不发布事件（事件由 ChatService 按返回的 dropped_requests 补发）。
        会话保持开启，后续 pending 请求继续执行。

        多 agent：同一请求内嵌套 agent 共享 session 级 stop_signal，随活跃请求一并停止；
        当前会话 worker 顺序处理请求，故 session 级信号等价于 per-request 信号。

        Returns:
            {stopped_request_ids, dropped_requests, active_step_ids}；
            session 不存在或已关闭返回 None。
        """
        target = str(request_id or "").strip()
        stop_reason = str(reason or "user-stop")

        # Phase 1：锁内完成状态变更并捕获任务引用（不持锁等待，避免阻塞池）
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or getattr(session, "closed", False):
                return None
            session.touch()

            stopped_task: asyncio.Task | None = None
            if target and target in session.active_tasks:
                # 活跃请求：设置 session 级 stop_signal，runtime 执行路径将抛 TurnStoppedError
                session.stop_signal.set()
                session.stop_reason = stop_reason
                stopped_task = session.active_tasks[target]

            dropped_requests: list[Any] = []
            async with session.request_lock:
                if session.pending_requests:
                    remaining: list[Any] = []
                    for req in list(session.pending_requests):
                        if str(getattr(req, "request_id", "")) == target:
                            dropped_requests.append(req)
                        else:
                            remaining.append(req)
                    session.pending_requests = remaining

        # Phase 2：锁外 grace 等待活跃任务自行走到检查点停止。
        # 只发信号不强制 cancel：强制 cancel 会走 worker 的 CancelledError→_close_session_state
        # 分支把整个会话关掉（误伤后续排队请求）；硬杀是 stop_session 的职责。
        # 超时（如 LLM 调用在飞）则留给下一个 raise_if_stop_signal 检查点处理，
        # SSE 侧 _wait_for_request_cleanup 会一直等到任务真正出队。
        if stopped_task is not None:
            await asyncio.wait([stopped_task], timeout=self.grace_stop_seconds)
            if not stopped_task.done():
                log.warning(
                    "stop_request 任务 {} 在 {}s grace 内未停止，交给下一个检查点处理"
                    "（如需立即终止请调用 stop_session）", target, self.grace_stop_seconds,
                )

        # Phase 3：仅在任务确已结束时兜底清信号（worker finally 也会清），
        # 任务尚在跑时必须保留 stop_signal 让检查点继续生效
        # 单 agent 链路退役：active_steps 已删除，active_step_ids 恒为空
        active_step_ids: list[str] = []
        session = await self.get(session_id)
        if session is not None and not getattr(session, "closed", False):
            if (
                stopped_task is not None
                and stopped_task.done()
                and session.stop_signal.is_set()
            ):
                session.stop_signal.clear()
                session.stop_reason = ""

        return {
            "stopped_request_ids": [target] if stopped_task is not None else [],
            "dropped_requests": dropped_requests,
            "active_step_ids": active_step_ids,
        }

    async def stop_session(
        self,
        session_id: str,
        *,
        reason: str = "user-stop",
        grace_seconds: float = 2.0,
    ) -> dict[str, Any] | None:
        """停止会话所有任务并关闭会话：设置 stop_signal、grace 等待活跃任务、关闭并移除会话。

        仅变更会话状态，不发布事件。活跃请求的停止事件由 runtime 自行发布，
        待处理请求由 ChatService 按返回的 dropped_requests 补发。复用 remove() 完成关闭+移除。

        多 agent：同请求内嵌套 agent 共享 stop_signal，随活跃请求一并停止；
        跨请求并发的场景（当前为顺序执行）需扩展 per-request 信号，目前无需处理。

        Returns:
            {stopped_request_ids, dropped_requests, active_step_ids, closed}；
            session 不存在或已关闭返回 None。
        """
        stop_reason = str(reason or "user-stop")

        # Phase 1：锁内设置 stop_signal、清空 pending、收集活跃任务与 step
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or getattr(session, "closed", False):
                return None
            session.touch()
            session.stop_signal.set()
            session.stop_reason = stop_reason

            active_items: list[tuple[str, asyncio.Task]] = list(session.active_tasks.items())
            # 单 agent 链路退役：active_steps 已删除，active_step_ids 恒为空
            active_step_ids: list[str] = []
            dropped_requests: list[Any] = []
            async with session.request_lock:
                dropped_requests = list(session.pending_requests)
                session.pending_requests = []
            tasks_to_wait = [t for _, t in active_items]

        # Phase 2：锁外 grace 等待活跃任务（stop_signal 已设，runtime 走 TurnStoppedError 自行发事件+落盘）
        if tasks_to_wait:
            await asyncio.wait(tasks_to_wait, timeout=grace_seconds)

        # Phase 3：关闭并移除会话（复用 remove：取消 worker + 残留活跃任务 + 标记 closed + pop）
        await self.remove(session_id)

        return {
            "stopped_request_ids": [rid for rid, _ in active_items],
            "dropped_requests": dropped_requests,
            "active_step_ids": active_step_ids,
            "closed": True,
        }

    async def active_count(self) -> int:
        """Get number of active non-closed sessions."""
        async with self._lock:
            return sum(
                1
                for session in self._sessions.values()
                if not getattr(session, "closed", False)
            )

    async def _cleanup_loop(self) -> None:
        """Periodically cleanup idle and closed sessions."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.cleanup_interval_seconds)
                await self._cleanup_inactive_sessions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("SessionPool cleanup error: {}", exc)

    async def _cleanup_inactive_sessions(self) -> None:
        """Remove sessions that are idle or already closed; swap-out the middle band."""
        now = time.time()
        sessions_to_remove = []
        swapped_sessions = []

        async with self._lock:
            for session_id, session in list(self._sessions.items()):
                # Remove already-closed sessions
                if getattr(session, "closed", False):
                    sessions_to_remove.append(session_id)
                    continue

                # Remove idle sessions
                last_activity = getattr(session, "last_activity_at", 0.0)
                idle_duration = now - last_activity
                if idle_duration > self.idle_timeout_seconds:
                    # 执行中的会话不回收：last_activity 只记录请求到达时间，
                    # 长任务执行期间（LLM 多轮编排可远超 idle 阈值）不能误杀，
                    # 留待任务结束后的下一轮 cleanup 再回收
                    if self._is_quiescent(session):
                        # 回收前保证摘要覆盖当前列表（fire-and-forget，后台任务持快照独立落盘）
                        cm = getattr(session, "context_manager", None)
                        if cm is not None and getattr(cm, "chat_messages", None):
                            await cm.ensure_summary_before_unload()
                        sessions_to_remove.append(session_id)
                        continue
                    log.info(
                        "SessionPool cleanup: session {} idle {:.0f}s 但仍有在途任务，跳过回收",
                        session_id, idle_duration,
                    )

                # P3 swap-out（docs/SystemApp架构设计.md §7）：闲置超过 swap 阈值且
                # 无在途任务 → 卸载 chat_messages（快照已在 turn_end 落盘），会话对象
                # 留在池中保持连续性；下次请求由 runtime 按需从快照恢复
                if (
                    self.swap_idle_seconds > 0
                    and idle_duration > self.swap_idle_seconds
                    and self._is_quiescent(session)
                ):
                    cm = getattr(session, "context_manager", None)
                    if cm is not None and cm.chat_messages:
                        # 卸载前保证摘要（fire-and-forget，后台任务持快照独立落盘）
                        await cm.ensure_summary_before_unload()
                        cm.chat_messages = []
                        # 已卸载状态由 chat_messages 为空体现，无需额外标记位
                        swapped_sessions.append(session_id)

            for session_id in sessions_to_remove:
                await self._close_session_unsafe(session_id)
                del self._sessions[session_id]

        if sessions_to_remove:
            log.info(
                "SessionPool cleanup: removed {} idle/closed sessions",
                len(sessions_to_remove),
            )
        if swapped_sessions:
            log.info(
                "SessionPool cleanup: swapped out {} idle sessions (chat_messages unloaded)",
                len(swapped_sessions),
            )

    @staticmethod
    def _is_quiescent(session: Any) -> bool:
        """会话当前无在途工作：无活跃请求任务、worker 空闲、不在处理中。"""
        if getattr(session, "processing", False):
            return False
        active_tasks = getattr(session, "active_tasks", {})
        if any(not task.done() for task in active_tasks.values()):
            return False
        worker_task = getattr(session, "worker_task", None)
        return worker_task is None or worker_task.done()

    async def _close_session_unsafe(self, session_id: str) -> None:
        """Close session without lock (caller must hold lock)."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        try:
            # Gracefully close worker task
            worker_task = getattr(session, "worker_task", None)
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                try:
                    await asyncio.wait_for(worker_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Cancel all active tasks
            active_tasks = getattr(session, "active_tasks", {})
            for task in active_tasks.values():
                if not task.done():
                    task.cancel()

            session.closed = True
        except Exception as exc:
            log.warning("Error closing session {}: {}", session_id, exc)

    def stats(self) -> dict[str, Any]:
        """Get pool statistics for monitoring."""
        # Note: This is a snapshot and doesn't hold lock
        active = sum(
            1
            for s in self._sessions.values()
            if not getattr(s, "closed", False)
        )
        return {
            "total_sessions": len(self._sessions),
            "active_sessions": active,
            "max_sessions": self.max_sessions,
            "idle_timeout_seconds": self.idle_timeout_seconds,
        }

    async def snapshot(self) -> list[dict[str, Any]]:
        """池内会话快照：状态按「关闭 / 处理中 / 空闲」三分，供管理端概览与检索。"""
        now = time.time()
        async with self._lock:
            items: list[dict[str, Any]] = []
            for session_id, session in self._sessions.items():
                closed = bool(getattr(session, "closed", False))
                active_tasks = len(getattr(session, "active_tasks", {}) or {})
                pending = len(getattr(session, "pending_requests", []) or [])
                processing = bool(getattr(session, "processing", False))
                if closed:
                    status = "closed"
                elif active_tasks or pending or processing:
                    status = "running"
                else:
                    status = "idle"
                ctx = getattr(session, "context_manager", None)
                last_active = float(getattr(session, "last_activity_at", 0) or 0)
                items.append(
                    {
                        "sessionId": session_id,
                        "appId": str(getattr(session, "app_id", "") or ""),
                        "userId": str(getattr(session, "user_id", "") or ""),
                        "agentName": str(getattr(session, "agent_name", "") or ""),
                        "status": status,
                        "activeTasks": active_tasks,
                        "pendingRequests": pending,
                        "messageCount": len(getattr(ctx, "chat_messages", []) or []),
                        "lastActivityAt": datetime.fromtimestamp(last_active).isoformat() if last_active else None,
                        "idleSeconds": int(now - last_active) if last_active else 0,
                    }
                )
            return items


def initialize_session_pool(system_app) -> SessionPool:
    """注册会话池组件（system_app.initialize_components 调用；关闭逆序保证会话池最后收尾）。"""
    return system_app.register(SessionPool)
