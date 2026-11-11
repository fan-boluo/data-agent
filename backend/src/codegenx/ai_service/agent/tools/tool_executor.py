"""工具执行器：统一执行 LLM 返回的 tool_calls，转换为可回填对话的 tool 消息。

执行前的确定性检查（存在性/配额/路径/熔断可用性/HITL 审批）都在 hook，执行器只负责
执行与记账；入参注入见本文件的 `inject_tool_context` / `build_call_kwargs`。
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

# 长耗时可并行工具（如 dispatch_agent 派发子智能体）：同轮多次调用 = 并行执行
# （gather + multi_agents.max_parallel 信号量限流）；其余工具保持串行
# （run_python 等共享执行容器，并行会冲突）
PARALLEL_TOOL_NAMES = {"dispatch_agent"}

# 工具入参注入：只补 session_id。
# 约定：工具不接收业务上下文（app_id/user_id/当前数据集等），执行器只告诉工具
# "当前是哪个会话"；工具侧用 `BaseTool` 同文件的 `ToolSession.load(params)` 按需解析。


def inject_tool_context(
    tool_input: dict[str, Any], session_state: "RuntimeSessionState | None",
) -> dict[str, Any]:
    """注入 session_id（LLM 显式传入时保留），其余上下文由工具自行按会话解析。"""
    if session_state is not None:
        tool_input.setdefault("session_id", session_state.session_id)
    return tool_input


def build_call_kwargs(
    func: Callable, tool_input: dict[str, Any],
    session_state: "RuntimeSessionState | None", stop_signal: Any,
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
    - dispatch_tool_calls：BaseReActAgent 工具循环的调度入口（并行/去重/顺序回填）
    - execute：注入 session_id → 分发 BEFORE 守卫 → 熔断准入 → 执行 → 记账 → AFTER 观测
    """

    # 组件名：system_app.tools_executor 属性按此名取实例
    name = ComponentType.Tool_EXECUTOR

    def __init__(self, system_app: "SystemApp | None" = None):
        super().__init__(system_app)

    def init_app(self, system_app: "SystemApp") -> None:
        # 无状态服务类，仅持有容器引用（工具查找时经 get_app() 取注册表）
        self.system_app = system_app

    async def async_before_start(self) -> None:
        """预热：为已注册工具创建熔断器，启动后 /metrics 即可看到全部工具状态。"""
        try:
            for tool in get_app().tool_registry.tools:
                get_tool_breaker(str(tool.name))
            log.info("[tool_breaker] 预热完成，共 {} 个工具熔断器", len(get_app().tool_registry.tools))
        except Exception as exc:  # 预热失败不阻断启动，首次真实调用再建
            log.warning("[tool_breaker] 预热失败（不阻断启动）: {}", exc)

    # 单个工具结果回填对话的最大字符数（防止撑爆上下文）
    MAX_TOOL_RESULT_CHARS = 8000

    async def dispatch_tool_calls(
        self, tool_calls: list[dict[str, Any]] | None,
        session_state: RuntimeSessionState | None = None,
    ) -> list[dict[str, Any]]:
        """执行 tool_calls 列表，返回 [{"role": "tool", ...}] 消息列表（与 tool_calls 顺序对应）。

        - PARALLEL_TOOL_NAMES 内的长耗时工具（如 dispatch_agent）同轮多次调用
          并行执行（gather + multi_agents.max_parallel 信号量限流），其余串行；
        - 同批参数完全相同的 tool_call 只执行一次、结果复用（防 LLM 重复调用）；
        - 停止信号（TurnStoppedError/CancelledError）透传终止请求，不吞成工具失败。
        """
        # 归一化：(name, arguments, tool_call_id, 去重 key)
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

        # 同批去重缓存：key → 执行结果（asyncio 单线程，闭包 dict 并发安全）
        cache: dict[str, ToolResult] = {}

        async def _run_one(name: str, arguments: dict, tc_id: str, key: str) -> ToolResult:
            if key in cache:
                return cache[key]
            try:
                result = await self.execute(
                    {"name": name, "arguments": arguments, "id": tc_id}, session_state)
            except (TurnStoppedError, asyncio.CancelledError):
                raise  # 停止信号传播终止请求
            except Exception as exc:
                log.error("工具 {} 执行异常: {}", name, exc)
                result = ToolResult(success=False, message=str(exc))
            cache[key] = result
            return result

        results_by_id: dict[str, ToolResult] = {}
        # 并行组：长耗时工具 gather 执行（信号量限流；停止信号经 gather 传播）
        parallel = [t for t in normalized if t[0] in PARALLEL_TOOL_NAMES]
        if parallel:
            semaphore = asyncio.Semaphore(max(1, int(config.multi_agents.max_parallel)))

            async def _one(item):
                async with semaphore:
                    return item[2], await _run_one(*item)

            for tc_id, result in await asyncio.gather(*(_one(i) for i in parallel)):
                results_by_id[tc_id] = result
        # 串行组：其余工具顺序执行
        for name, arguments, tc_id, key in normalized:
            if tc_id not in results_by_id:
                results_by_id[tc_id] = await _run_one(name, arguments, tc_id, key)

        # 统一转换为 tool 消息（按原 tool_calls 顺序）
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

    async def execute(self, tool_call: dict[str, Any],
                      session_state: RuntimeSessionState | None = None) -> ToolResult:
        """单工具执行入口：注入上下文 → BEFORE 守卫 → 熔断准入 → 执行 → 记账 → AFTER 观测。

        - BEFORE：serial + 可短路（许可规则/HITL 审批等），blocked 时直接把消息回填为失败结果；
        - 熔断：守卫已做可用性快判，这里 acquire 负责半开探测名额；
        - trace：with start_span 包裹执行骨架（op_type=注册表 obs_category），
          入参/结果明细由 AFTER_TOOL_CALL 监听器落 telemetry.tool_semantics。
        """
        tool_name = str(tool_call.get("name") or "")
        tool_input = inject_tool_context(dict(tool_call.get("arguments") or {}), session_state)
        app_id = str(session_state.request.app_id) if session_state is not None and session_state.request is not None else "main"
        session_id = getattr(session_state, "session_id", "") if session_state is not None else ""
        stop_signal = getattr(session_state, "stop_signal", None) if session_state is not None else None

        # 1. BEFORE_TOOL_CALL 守卫（入参注入后触发，守卫能看到完整参数）
        turn = getattr(session_state, "activate_turn", None) if session_state is not None else None
        before_ctx = HookContext(event=HookEvent.BEFORE_TOOL_CALL, session=session_state, turn=turn)
        before_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        await hook_manager.dispatch(HookEvent.BEFORE_TOOL_CALL, before_ctx)
        # 守卫可改写参数（如审批人收窄 SQL）：patches 合并后按新参数执行
        override = before_ctx.patches.get("tool_arguments")
        if isinstance(override, dict) and override:
            tool_input.update(override)
            before_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        if before_ctx.action == "blocked":
            error_class = str(before_ctx.data.get("error_class") or "arg_invalid")
            log.info("工具 {} 被 BEFORE_TOOL_CALL 守卫阻断: {}", tool_name, before_ctx.message)
            # 守卫阻断同样是一次失败调用（参数/权限/审批/配额/熔断），补打点避免静默
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name,
                           is_error=True, latency_ms=0.0, error_class=error_class)
            return ToolResult(
                success=False,
                error_class=error_class,
                message=before_ctx.message or "Tool blocked by hook",
            )

        # 2. 查找工具（存在性已由守卫校验，这里只做兜底）
        tool = next((t for t in get_app().tool_registry.tools if t.name == tool_name), None)
        if not tool:
            log.error("Unknown tool called after guard: {}", tool_name)
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name or "unknown",
                           is_error=True, latency_ms=0.0, error_class="arg_invalid")
            return ToolResult(success=False, message=f"工具「{tool_name}」不存在，本次调用未执行。")

        # 3. 熔断准入（守卫已对冷却期内的 open 态快速失败，这里只占半开探测名额）
        if not await acquire_slot(tool_name):
            feed_tool_call(app_id=app_id, session_id=session_id, tool_name=tool_name,
                           is_error=True, latency_ms=0.0, error_class="circuit_open")
            log.warning("[tool_call] tool={} status=circuit_open app_id={} session_id={}",
                        tool_name, app_id, session_id)
            return ToolResult(
                success=False,
                message=f"工具 {tool_name} 暂时不可用（熔断打开），请稍后重试或改用其他工具",
            )

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
        span_input = {"tool_call_id": str(tool_call.get("id") or ""), **tool_input}
        async with start_span(str(tool.obs_category or "tool"), tool_name,
                              input_data=span_input) as span:
            try:
                result = func(**call_kwargs)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout)
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

        return result


def initialize_tool_executor(system_app) -> ToolExecutor:
    """启动装配入口：注册工具执行器组件（DB-GPT 风格 initialize_xxx 约定）。"""
    return system_app.register(ToolExecutor)
