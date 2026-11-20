"""工具执行器：统一执行 LLM 返回的 tool_calls，转换为可回填对话的 tool 消息。

执行前的确定性检查（存在性/配额/路径/熔断可用性/HITL 审批）都在 hook，执行器只负责
执行与记账；入参注入见本文件的 `inject_tool_context` / `build_call_kwargs`。

并行化要点（P1-2）：
- 全局并行由 config.tools.execution_mode 开关驱动，默认 parallel；sequential 保留方便切换。
- 两级进程级信号量隔离死锁：
    * agent_slots  —— dispatch_agent 专用（多 agent.max_parallel）；
    * tool_slots   —— 其余工具含子 agent 内部工具（tools.max_concurrency）。
  子 agent 不能再派发子 agent，嵌套深度恒为 1，不会环形死锁。
- 同批参数相同的 tool_call 复用同一份执行结果（parallel 用 asyncio.Task 缓存防竞态，
  sequential 用普通 dict 即可）。
- 停止信号（TurnStoppedError/CancelledError）透传终止请求，不吞成工具失败；
  工具执行统一走 `_await_with_stop`，停止时取消工具任务，长耗时工具（SQL/pandas）
  无需各自轮询 signal 也能被中断。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.tools.base import ToolResult
from codegenx.ai_service.agent.agent_schema import AgentEventType, AgentState
from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState, TurnStoppedError
# 工具执行前的确定性检查（存在性/配额/路径/熔断可用性/HITL 审批）均为
# BEFORE_TOOL_CALL 监听器，import 即完成注册；执行器只做执行与记账
from codegenx.ai_service.agent.tools import tool_guards  # noqa: F401
from codegenx.ai_service.agent.tools.tool_circuit import (
    acquire_slot,
    get_tool_breaker,
    record_outcome,
    release_slot,
)
from codegenx.ai_service.hook import HookContext, HookEvent, hook_manager
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.trace import start_span
from codegenx.ai_service.trace.metrics import feed_tool_call
from codegenx.ai_service.utils.config import config
from shared import log

if TYPE_CHECKING:
    from codegenx.ai_service.system_app import SystemApp


# 工具入参注入：只补 session_id。
# 约定：工具不接收业务上下文（app_id/user_id/当前数据集等），执行器只告诉工具
# "当前是哪个会话"；工具侧用 `BaseTool` 同文件的 `ToolSession.load(params)` 按需解析。


def inject_tool_context(
    tool_input: dict[str, Any], session_state: "RuntimeSessionState",
) -> dict[str, Any]:
    """注入 session_id（LLM 显式传入时保留），其余上下文由工具自行按会话解析。"""
    tool_input.setdefault("session_id", session_state.session_id)
    return tool_input


def build_call_kwargs(
    func: Callable, tool_input: dict[str, Any],
    session_state: RuntimeSessionState, stop_signal: Any,
) -> dict[str, Any]:
    """按工具签名组装执行参数：声明了 signal/session_state 的工具才注入。

    约定：params 必传；signal 供长耗时/可中断工具感知终止；session_state 供需要
    看板握手、结果入史等会话能力的编排类工具（如 dispatch_agent）使用。
    """
    params = inspect.signature(func).parameters
    kwargs: dict[str, Any] = {"params": tool_input}
    if "signal" in params:
        kwargs["signal"] = stop_signal
    if "session_state" in params:
        kwargs["session_state"] = session_state
    return kwargs


class ToolExecutor(BaseComponent):
    """工具执行器：统一执行 LLM 返回的 tool_calls，转换为可回填对话的 tool 消息。

    职责边界（执行前的校验/权限/审批都在 hook，不在这里）：
    - dispatch_tool_calls：BaseReActAgent 工具循环的调度入口（按 config.tools.execution_mode
      分流到 execute_parallel / execute_sequence）；
    - execute：注入 session_id → BEFORE 守卫 → 熔断准入 → 执行 → 记账 → AFTER 观测；
    - 进程级两级信号量在 init_app 初始化，dispatch_agent 占 agent_slots，其余占 tool_slots。
    """

    name = ComponentType.Tool_EXECUTOR

    # 单个工具结果回填对话的最大字符数（防止撑爆上下文）
    MAX_TOOL_RESULT_CHARS = 8000

    def __init__(self, system_app: "SystemApp | None" = None):
        super().__init__(system_app)
        # 进程级两级信号量：子 agent 不派发子 agent，嵌套深度 1 无环，不会死锁
        self._agent_slots: asyncio.Semaphore = asyncio.Semaphore(1)
        self._tool_slots: asyncio.Semaphore = asyncio.Semaphore(1)

    def init_app(self, system_app: "SystemApp") -> None:
        self.system_app = system_app
        # 子agent并行数：3
        self._agent_slots = asyncio.Semaphore(max(1, int(config.multi_agents.max_parallel)))
        # 工具执行并发上限（含子 agent 内部工具) 默认5
        self._tool_slots = asyncio.Semaphore(max(1, int(config.tools.max_concurrency)))

    async def async_before_start(self) -> None:
        """预热：为已注册工具创建熔断器，启动后 /metrics 即可看到全部工具状态。"""
        try:
            for tool in get_app().tool_registry.tools:
                get_tool_breaker(str(tool.name))
            log.info("[tool_breaker] 预热完成，共 {} 个工具熔断器", len(get_app().tool_registry.tools))
        except Exception as exc:
            log.warning("[tool_breaker] 预热失败（不阻断启动）: {}", exc)

    # ------------------------------------------------------------------ 调度入口
    async def dispatch_tool_calls(
        self, tool_calls: list[dict[str, Any]],
        session_state: RuntimeSessionState,
    ) -> list[dict[str, Any]]:
        """按 config.tools.execution_mode 分流调度器（parallel=全部并行，sequential=全部串行）。"""
        mode = str(config.tools.execution_mode or "parallel").strip().lower()
        if mode == "sequential":
            return await self.execute_sequence(tool_calls, session_state)
        return await self.execute_parallel(tool_calls, session_state)

    # ------------------------------------------------------------------ 两版调度器
    async def execute_parallel(
        self, tool_calls: list[dict[str, Any]],
        session_state: RuntimeSessionState,
    ) -> list[dict[str, Any]]:
        """并行版：全部工具 gather + future 去重 + tool_slots 限流。

        dispatch_agent（长耗时派发）额外 agent_slots 限流，防嵌套死锁。
        """
        normalized = self._normalize_and_dedupe(tool_calls)
        # future 缓存：首见 key 创建 task，后来者 await 同一 task 防并发竞态
        cache: dict[str, asyncio.Task[ToolResult]] = {}

        async def _run_one(name: str, arguments: dict, tc_id: str, key: str) -> ToolResult:
            task = cache.get(key)
            if task is None:
                task = asyncio.create_task(
                    self.execute({"name": name, "arguments": arguments, "id": tc_id}, session_state)
                )
                cache[key] = task
            # 停止信号透传；普通异常收口为失败结果（由 execute 内部处理）
            return await task

        async def _throttled(item) -> tuple[str, ToolResult]:
            name, arguments, tc_id, key = item
            # dispatch_agent 只占 agent_slots（不占 tool_slots），否则同轮多个派发会把
            # 工具席位吃光，子 agent 内部工具等不到席位 → 嵌套死锁
            if name == "dispatch_agent":
                async with self._agent_slots:
                    return tc_id, await _run_one(name, arguments, tc_id, key)
            async with self._tool_slots:
                return tc_id, await _run_one(name, arguments, tc_id, key)

        # gather 任一工具抛 TurnStoppedError/CancelledError 整体取消；普通异常已收口为 ToolResult
        results_by_id: dict[str, ToolResult] = {}
        for tc_id, result in await asyncio.gather(*(_throttled(t) for t in normalized)):
            results_by_id[tc_id] = result
        return self._build_tool_messages(normalized, results_by_id)

    async def execute_sequence(
        self, tool_calls: list[dict[str, Any]],
        session_state: RuntimeSessionState,
    ) -> list[dict[str, Any]]:
        """顺序版：for 循环串行执行 + tool_slots 限流 + 去重（dict 缓存即可，无并发竞态）。"""
        normalized = self._normalize_and_dedupe(tool_calls)
        cache: dict[str, ToolResult] = {}
        results_by_id: dict[str, ToolResult] = {}
        for name, arguments, tc_id, key in normalized:
            if key in cache:
                results_by_id[tc_id] = cache[key]
                continue
            async with self._tool_slots:
                result = await self.execute(
                    {"name": name, "arguments": arguments, "id": tc_id}, session_state)
            cache[key] = result
            results_by_id[tc_id] = result
        return self._build_tool_messages(normalized, results_by_id)

    # ------------------------------------------------------------------ 公共 helper
    @staticmethod
    def _normalize_and_dedupe(
        tool_calls: list[dict[str, Any]],
    ) -> list[tuple[str, dict, str, str]]:
        """归一化：(name, arguments, tool_call_id, 去重 key)；同批完全相同参数只执行一次。"""
        normalized: list[tuple[str, dict, str, str]] = []
        for tc in tool_calls or []:
            name = str(tc.get("name") or "")
            arguments = tc.get("arguments") or {}
            # 部分模型把 arguments 输出成 JSON 字符串，兜底解析
            if isinstance(arguments, str):
                with suppress(Exception):
                    arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                arguments = {}
            # 去重 key：参数序列化后剔除全部空白（仅空白差异视为同一调用）
            key = re.sub(r"\s+", "", name + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            normalized.append((name, arguments, str(tc.get("id") or ""), key))
        return normalized

    def _build_tool_messages(
        self, normalized: list[tuple[str, dict, str, str]],
        results_by_id: dict[str, ToolResult],
    ) -> list[dict[str, Any]]:
        """按 normalized 顺序把 ToolResult 转换为 tool 消息（与 tool_calls 顺序对应）。"""
        results: list[dict[str, Any]] = []
        for name, _arguments, tc_id, _key in normalized:
            result = results_by_id[tc_id]
            # 约定：仅按 success 字段判定执行状态；成功回传 data，失败优先回传原因
            content = result.data if result.success else (result.message or result.data or "工具执行失败")
            if len(content) > self.MAX_TOOL_RESULT_CHARS:
                content = content[: self.MAX_TOOL_RESULT_CHARS] + (
                    f"\n...(已截断，原长 {len(content)} 字符)"
                )
            log.info("工具执行完成: {} | success={} | content_len={}", name, result.success, len(content))
            results.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": content,
                # 内部记账字段：供调用方统计工具成败（回填对话前剔除，不进 LLM API）
                "_success": result.success,
            })
        return results

    # ------------------------------------------------------------------ 停止感知等待
    async def _await_with_stop(
        self, awaitable: Any, stop_signal: Any,
        timeout: float | None, stop_reason: str = "",
    ) -> Any:
        """等待工具结果，同时监听停止信号：工具内不必各自轮询 signal。

        - 工具先完成：返回结果（异常原样抛出，含工具自身抛的 TurnStoppedError）；
        - 停止先到：取消工具任务并抛 TurnStoppedError（asyncpg 取消查询、线程任务被丢弃）；
        - 等待超时：取消工具任务并抛 TimeoutError（行为与原 wait_for 一致）。
        """
        tool_task = asyncio.ensure_future(awaitable)
        stop_task: asyncio.Task | None = None
        if stop_signal is not None and hasattr(stop_signal, "wait"):
            stop_task = asyncio.create_task(stop_signal.wait())
        waiters = [t for t in (tool_task, stop_task) if t is not None]
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # 工具先结束时回收未触发的等待任务
            if stop_task is not None and not stop_task.done():
                stop_task.cancel()

        if tool_task in done:
            return tool_task.result()
        # 停止/超时：取消工具；回调里消费结果，避免 "Task exception was never retrieved"
        tool_task.cancel()
        tool_task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if stop_task is not None and stop_task in done:
            raise TurnStoppedError(stop_reason or "user-stop")
        raise asyncio.TimeoutError()

    # ------------------------------------------------------------------ 单工具执行入口
    async def execute(self, tool_call: dict[str, Any],
                      session_state: RuntimeSessionState) -> ToolResult:
        """单工具执行入口：注入上下文 → BEFORE 守卫 → 熔断准入 → 执行 → 记账 → AFTER 观测。

        - BEFORE：serial + 可短路（许可规则/HITL 审批等），blocked 时直接把消息回填为失败结果；
        - 熔断：守卫已做可用性快判，这里 acquire 负责半开探测名额；
        - trace：with start_span 包裹执行骨架（op_type=注册表 obs_category），
          入参/结果明细由 AFTER_TOOL_CALL 监听器落 telemetry.tool_semantics；
        - SSE 事件：TOOL_EXECUTION_START 找到工具后发、TOOL_EXECUTION_END 在所有 return 路径发
          （含 blocked/circuit_open/tool_not_found 三种执行前失败态），前端实时感知工具生命周期。
        """
        tool_name = str(tool_call.get("name") or "")
        tool_input = inject_tool_context(dict(tool_call.get("arguments") or {}), session_state)
        app_id = str(session_state.request.app_id) if session_state.request is not None else "main"
        session_id = getattr(session_state, "session_id", "")
        stop_signal = getattr(session_state, "stop_signal", None)
        tool_call_id = str(tool_call.get("id") or "")

        # 已收到停止信号：不再启动新工具，直接终止本轮
        if stop_signal is not None and stop_signal.is_set():
            raise TurnStoppedError(str(getattr(session_state, "stop_reason", "") or "user-stop"))

        # 1. BEFORE_TOOL_CALL 守卫（入参注入后触发，守卫能看到完整参数）
        turn = getattr(session_state, "activate_turn", None)
        before_ctx = HookContext(event=HookEvent.BEFORE_TOOL_CALL, session=session_state, turn=turn)
        before_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        await hook_manager.dispatch(HookEvent.BEFORE_TOOL_CALL, before_ctx)
        # 守卫可改写参数（如审批人收窄 SQL）：patches 合并后按新参数执行
        override = before_ctx.patches.get("tool_arguments")
        if isinstance(override, dict) and override:
            tool_input.update(override)
            before_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}

        # ── 执行前失败：只发 END（失败态），不发 START ──
        if before_ctx.action == "blocked":
            error_class = str(before_ctx.data.get("error_class") or "arg_invalid")
            log.info("工具 {} 被 BEFORE_TOOL_CALL 守卫阻断: {}", tool_name, before_ctx.message)
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name,
                           is_error=True, latency_ms=0.0, error_class=error_class)
            r = ToolResult(success=False, error_class=error_class,
                           message=before_ctx.message or "Tool blocked by hook")
            await self._publish_tool_end(session_state, tool_name, tool_call_id, r, state=AgentState.FAILED, tool_input=tool_input)
            return r

        # 2. 查找工具（存在性已由守卫校验，这里只做兜底）
        tool = next((t for t in get_app().tool_registry.tools if t.name == tool_name), None)
        if not tool:
            log.error("Unknown tool called after guard: {}", tool_name)
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name or "unknown",
                           is_error=True, latency_ms=0.0, error_class="arg_invalid")
            r = ToolResult(success=False, message=f"工具「{tool_name}」不存在，本次调用未执行。")
            await self._publish_tool_end(session_state, tool_name, tool_call_id, r, state=AgentState.FAILED, tool_input=tool_input)
            return r

        # 3. 熔断准入（守卫已对冷却期内的 open 态快速失败，这里只占半开探测名额）
        if not await acquire_slot(tool_name):
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name,
                           is_error=True, latency_ms=0.0, error_class="circuit_open")
            log.warning("[tool_call] tool={} status=circuit_open app_id={} session_id={}",
                        tool_name, app_id, session_id)
            r = ToolResult(success=False, error_class="circuit_open",
                           message=f"工具 {tool_name} 暂时不可用（熔断打开），请稍后重试或改用其他工具")
            await self._publish_tool_end(session_state, tool_name, tool_call_id, r, state=AgentState.FAILED, tool_input=tool_input)
            return r

        # ── 找到工具，开始执行：先发 START ──
        await self._publish_tool_start(session_state, tool_name, tool_call_id, tool_input)

        # 4. 工具执行（trace 直埋：注册表 obs_category 决定 op_type——tool/retrieval；
        #    with 块天然维护 parent 栈，工具内派发的子 agent/llm span 挂本 span 下）
        func = tool.executor
        call_kwargs = build_call_kwargs(func, tool_input, session_state, stop_signal)
        # 工具级超时（配置 0 = 关闭，沿用工具内部超时）
        circuit_cfg = config.tools.circuit
        timeout = circuit_cfg.timeout_seconds if circuit_cfg.enabled and circuit_cfg.timeout_seconds > 0 else None
        t0 = time.monotonic()
        error_class = ""
        # tool span input 携带 LLM 调用 id（协议溯源，与 llm span output.tool_calls[].id 对账）
        span_input = {"tool_call_id": tool_call_id, **tool_input}

        result: ToolResult
        span_id: str | None = None
        try:
            async with start_span(str(tool.obs_category or "tool"), tool_name,
                                  input_data=span_input) as span:
                try:
                    result = func(**call_kwargs)
                    if inspect.isawaitable(result):
                        # 统一停止感知：停止信号优先于工具执行完成，长耗时工具可被中断
                        result = await self._await_with_stop(
                            result, stop_signal, timeout,
                            stop_reason=str(getattr(session_state, "stop_reason", "") or ""),
                        )
                except (TurnStoppedError, asyncio.CancelledError):
                    await release_slot(tool_name)  # 取消无结果：归还半开探测名额
                    raise
                except Exception as exc:
                    # 异常统一收口为失败结果：后续指标/日志/熔断记账不会被跳过
                    error_class = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else "execution"
                    log.warning("[tool_call] tool={} status=error class={} app_id={} session_id={} err={}: {}",
                                tool_name, error_class, app_id, session_id, type(exc).__name__, exc)
                    result = ToolResult(success=False, message=f"{type(exc).__name__}: {exc}")
                # 骨架收尾：输出概要 + 成败（语义字段由 AFTER 监听器单独落表）
                message = str(getattr(result, "message", "") or "")
                span.set_output({
                    "success": bool(result.success),
                    "message": message,
                    "data": str(getattr(result, "data", "") or ""),
                })
                span.set_status(ok=bool(result.success), error=message or None)
                span_id = span.span["span_id"] if span.span else None
        finally:
            # TurnStoppedError/CancelledError 会跳过 finally 里的后续记账（下面 return 之前还有步骤），
            # 但这两个异常会在 execute_parallel 的 gather 层被捕获并透传，
            # 不影响前端收到前面已发的 START 事件（结束态由上层 REQUEST_STOPPED 兜底）
            pass

        # 5. 熔断记账 + 失败分类归一（口径见 tool_circuit.record_outcome）
        error_class = error_class or str(getattr(result, "error_class", "") or "")
        error_class = await record_outcome(
            tool_name, success=result.success, error_class=error_class,
            message=str(getattr(result, "message", "") or ""),
        )

        latency_ms = (time.monotonic() - t0) * 1000
        feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name,
                       is_error=not result.success, latency_ms=latency_ms,
                       error_class=error_class if not result.success else "")
        log.debug("[tool_call] tool={} status={} class={} latency_ms={:.1f} app_id={} session_id={}",
                  tool_name, "ok" if result.success else "error", error_class or "-",
                  latency_ms, app_id, session_id)

        # 6. AFTER_TOOL_CALL 钩子（parallel 容错分发：语义落表/落盘/记忆信号等）
        after_ctx = HookContext(event=HookEvent.AFTER_TOOL_CALL, session=session_state, turn=turn)
        after_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        after_ctx.data["result"] = result
        after_ctx.data["span_id"] = span_id
        await hook_manager.dispatch(HookEvent.AFTER_TOOL_CALL, after_ctx)

        # ── 执行结束：发 END（成功或失败） ──
        state = AgentState.SUCCESS if result.success else AgentState.FAILED
        await self._publish_tool_end(session_state, tool_name, tool_call_id, result, state=state, tool_input=tool_input)

        return result

    # ------------------------------------------------------------------ 前端感知事件
    @staticmethod
    async def _publish_tool_start(
        session_state: RuntimeSessionState,
        tool_name: str, tool_call_id: str, tool_input: dict,
    ) -> None:
        """工具开始执行事件（前端感知"正在执行什么工具"）。

        延迟 import + 吞异常：事件发送失败不影响执行链路。
        """
        try:
            from codegenx.ai_service.system_app import get_app
            await get_app().message_bus.publish_runtime_event(
                session_state.publish_context,
                event_type=AgentEventType.TOOL_EXECUTION_START,
                state=AgentState.RUNNING,
                data={"name": tool_name, "id": tool_call_id, "arguments": tool_input},
            )
        except Exception as exc:
            log.warning("TOOL_EXECUTION_START 发布失败: {}", exc)

    @staticmethod
    async def _publish_tool_end(
        session_state: RuntimeSessionState,
        tool_name: str, tool_call_id: str,
        result: ToolResult, state: AgentState,
        tool_input: dict | None = None,
    ) -> None:
        """工具执行结束事件（前端感知"工具执行完毕，成功/失败"）。

        SQL 类工具额外携带 sql/row_count/rows_preview，前端据此把工具节点渲染成 SQL 卡片。
        """
        try:
            from codegenx.ai_service.system_app import get_app
            from codegenx.ai_service.agent.agent_schema import MessageType

            extras = getattr(result, "extras", None) or {}
            images = list(extras.get("images") or [])
            report = extras.get("report")
            chart_option = extras.get("chart_option")
            data = {
                "name": tool_name,
                "tool_call_id": tool_call_id,
                "success": bool(result.success),
                "message": str(getattr(result, "message", "") or ""),
                "error_class": str(getattr(result, "error_class", "") or ""),
                "state": "success" if result.success else "failed",
                "render": str(getattr(result, "render", "") or ""),
                # chart_option：ECharts 配置（前端交互式渲染）；images：普通图片相对路径
                "chart_option": chart_option,
                "images": images,
                "report": report,
            }
            # SQL 卡片数据：仅 SQL 类工具（入参含 sql）附加，预览行取自 extras，不进 LLM 上下文
            sql = str((tool_input or {}).get("sql") or "")
            if sql:
                data["sql"] = sql
                data["row_count"] = int(extras.get("row_count") or 0)
                data["rows_preview"] = extras.get("rows_preview") or []
            # 消息类型推断：报告 > 交互式图表 > 普通图片 > 纯文本
            if not result.success:
                message_type = MessageType.TEXT
            elif report:
                message_type = MessageType.REPORT
            elif chart_option:
                message_type = MessageType.CHART
            elif images:
                message_type = MessageType.IMAGE
            else:
                message_type = MessageType.TEXT
            await get_app().message_bus.publish_runtime_event(
                session_state.publish_context,
                event_type=AgentEventType.TOOL_EXECUTION_END,
                state=state,
                data=data,
                message_type=message_type,
            )
        except Exception as exc:
            log.warning("TOOL_EXECUTION_END 发布失败: {}", exc)


def initialize_tool_executor(system_app) -> ToolExecutor:
    """启动装配入口：注册工具执行器组件（DB-GPT 风格 initialize_xxx 约定）。"""
    return system_app.register(ToolExecutor)
