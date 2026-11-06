"""LLM 韧性层（熔断 + 重试 + 模型降级链，见 docs/LLM调用设计方案.md）。

统一职责（模型选择不在本层——各组件初始化时从自身配置解析模型链后传入）：
  1. CircuitBreaker  三态熔断器，按 (provider, model) 粒度，asyncio 安全；
  2. resilient_invoke         非流式执行：同模型退避重试 → 逐级 fallback；
  3. resilient_invoke_stream  流式执行：仅首 chunk 前允许透明重试/换模型
                              （首 chunk 后失败直接抛，避免向用户重复吐字）；
  4. 观测            label/model/outcome 指标 + 结构化调用日志。

错误处理契约（llm/errors.py 四分类）：
  RETRYABLE         同模型退避重试（尊重 Retry-After），耗尽后切 fallback；
  CONTEXT_OVERFLOW  不重试直接切 fallback（更长的模型可能救回来）；无 fallback 时抛给
                    调用方（agent 侧压缩历史后重试）；
  FATAL             不重试，切 fallback（换 provider 可能解决 401/404），不计熔断失败；
  LOGIC             本地 bug，立即抛出，绝不能被重试/降级掩盖。
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence

from shared import log

from codegenx.ai_service.llm.async_client import get_llm
from codegenx.ai_service.llm.errors import LLMErrorClass, classify_llm_error
from codegenx.ai_service.trace import current_app_id, get_current_agent, get_turn_binding, start_span
from codegenx.ai_service.utils.config import config


class LLMNoModelAvailableError(RuntimeError):
    """链上所有模型均不可用（熔断打开或重试/降级耗尽）。"""


# ── 指标埋点（守卫式：埋点绝不阻断业务）────────────────────────────────────

def _metrics():
    from codegenx.ai_service.trace import metrics as m
    return m


def _inc(name: str, **labels) -> None:
    try:
        getattr(_metrics(), name).labels(**labels).inc()
    except Exception:  # noqa: BLE001
        pass


def _set_gauge(name: str, value: float, **labels) -> None:
    try:
        getattr(_metrics(), name).labels(**labels).set(value)
    except Exception:  # noqa: BLE001
        pass


def _record_usage(model: str, usage: Any) -> None:
    """LLM token 用量唯一喂入点（守卫式，绝不阻断调用）：结构化日志 + Prometheus 计数。

    韧性层是全部 LLM 调用的统一出口（在线 think 流式/非流式、compact/summary/memory），
    在此采集天然全覆盖且不重复；app_id 从 trace turn 上下文归因
    （在线轮次=真实 app，后台任务=system）。
    """
    if not usage:
        return
    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    else:
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
    if not prompt and not completion:
        return
    log.debug(
        "[llm_usage] model={} prompt_tokens={} completion_tokens={}",
        model, prompt, completion,
    )
    try:
        from codegenx.ai_service.trace import current_app_id
        m = _metrics()
        app_id = current_app_id()
        if prompt:
            m.llm_prompt_tokens_total.labels(app_id=app_id, model=model).inc(prompt)
        if completion:
            m.llm_completion_tokens_total.labels(app_id=app_id, model=model).inc(completion)
    except Exception:  # noqa: BLE001 — 埋点失败静默
        pass


def _usage_ints(usage: Any) -> tuple[int, int]:
    """usage（dict/对象双兼容）→ (prompt_tokens, completion_tokens)。"""
    if not usage:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


def _record_call(trigger: str, model: str, cls: Optional[LLMErrorClass],
                 total_ms: float = 0.0, first_token_ms: float = 0.0) -> None:
    """LLM 调用 attempt 唯一喂入（守卫式）：计数含失败分类，trigger=触发来源。

    每次 attempt（成功+失败）喂一次；app_id 按 turn 上下文归因
    （在线=真实 app，后台任务=system）；延迟/首 token 仅成功（cls=None）时 observe。
    """
    try:
        _metrics().feed_llm_attempt(
            app_id=current_app_id(), model=model, trigger=trigger,
            outcome="ok" if cls is None else f"error_{cls.value}",
            total_ms=total_ms, first_token_ms=first_token_ms,
        )
    except Exception:  # noqa: BLE001 — 埋点失败静默
        pass


def _llm_span(model: str, messages: List[Dict[str, Any]]):
    """llm span（观测直埋）：name 取当前 agent 域；无 turn 绑定时 no-op。

    input 防膨胀：在线问答（chat_messages 为真相源）存引用——messages_ref +
    message_count；上下文 token 涨落由 attributes.prompt_tokens 序列体现
    （服务端精确值），中间过程（think/tool_calls）在本 trace spans。
    后台合成消息内联截断。
    """
    binding = get_turn_binding()
    if binding is not None and not binding.inline_input:
        input_data: Dict[str, Any] = {
            "messages_ref": "chat_messages",
            "message_count": len(messages),
        }
    else:
        input_data = {"messages": messages}
    return start_span("llm", get_current_agent() or "llm", input_data=input_data)


# ── 三态熔断器 ──────────────────────────────────────────────────────────────

class CircuitBreaker:
    """closed →(连续失败≥阈值)→ open →(冷却期)→ half_open →(探测达标)→ closed。

    asyncio 单线程内状态迁移经过 asyncio.Lock 串行化，防止未来加入 await 点后
    出现 check-then-act 竞态。
    """

    _STATE_CODE = {"closed": 0, "half_open": 1, "open": 2}

    def __init__(
        self,
        key: str,
        failure_threshold: int,
        recovery_timeout: float,
        half_open_max_calls: int,
        half_open_success_rate: float,
    ) -> None:
        self.key = key  # "provider:model"
        self._failure_threshold = max(1, failure_threshold)
        # 下限 0.01：防 0/负值导致立即重开；生产配置 30s，测试可用小值
        self._recovery_timeout = max(0.01, recovery_timeout)
        self._half_open_max_calls = max(1, half_open_max_calls)
        self._half_open_success_rate = half_open_success_rate
        self._lock = asyncio.Lock()
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_calls = 0
        self._half_success = 0
        self._half_total = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def state_code(self) -> int:
        """状态对应的指标数值（0=closed 1=half_open 2=open）。"""
        return self._STATE_CODE[self._state]

    async def acquire(self) -> bool:
        """请求进入前调用。False = 快速失败，调用方应立即走 fallback。"""
        async with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self._recovery_timeout:
                    self._enter("half_open")  # 冷却结束，放行有限探测
                else:
                    return False
            if self._state == "half_open":
                if self._half_calls >= self._half_open_max_calls:
                    return False  # 探测名额已满
                self._half_calls += 1
            return True

    async def record_success(self) -> None:
        async with self._lock:
            if self._state == "half_open":
                self._half_success += 1
                self._half_total += 1
                if self._half_total >= 2 and (
                    self._half_success / self._half_total >= self._half_open_success_rate
                ):
                    self._enter("closed")  # 探测达标，恢复
            elif self._state == "closed":
                self._consecutive_failures = 0

    async def record_failure(self, count_as_failure: bool = True) -> None:
        """记录失败；half_open 下任何失败都收敛为 open（归还探测名额）。

        count_as_failure=False 用于 FATAL/OVERFLOW/LOGIC：闭态不累计连续失败，
        但半开探测必须收敛，否则探测名额耗尽后熔断器会永久卡在 half_open。
        """
        async with self._lock:
            if self._state == "half_open":
                self._half_total += 1
                self._enter("open")  # 探测失败，重新打开
            elif self._state == "closed" and count_as_failure:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    self._enter("open")

    async def release(self) -> None:
        """归还半开探测名额（请求被取消等无结果场景），不改变熔断状态。"""
        async with self._lock:
            if self._state == "half_open":
                self._half_calls = max(0, self._half_calls - 1)

    def _enter(self, state: str) -> None:
        self._state = state
        if state == "open":
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0
            log.warning("[llm_breaker] {} OPEN（连续失败或探测失败）", self.key)
            _inc("llm_breaker_opens_total", circuit=self.key)
        elif state == "closed":
            log.info("[llm_breaker] {} CLOSED（探测恢复）", self.key)
        elif state == "half_open":
            log.info("[llm_breaker] {} HALF_OPEN（冷却结束，放行探测）", self.key)
        self._half_calls = self._half_success = self._half_total = 0
        _set_gauge("llm_circuit_state", self._STATE_CODE[state], circuit=self.key)


_breakers: Dict[str, CircuitBreaker] = {}


def get_breaker(model_name: str) -> CircuitBreaker:
    """取（或创建）模型级熔断器，键为 provider:model（模型故障是全局性的）。"""
    provider = config.get_provider_name_by_model_name(model_name)
    key = f"{provider}:{model_name}"
    breaker = _breakers.get(key)
    if breaker is None:
        cfg = config.llm
        breaker = CircuitBreaker(
            key,
            failure_threshold=cfg.breaker_failure_threshold,
            recovery_timeout=cfg.breaker_recovery_timeout_seconds,
            half_open_max_calls=cfg.breaker_half_open_max_calls,
            half_open_success_rate=cfg.breaker_half_open_success_rate,
        )
        _breakers[key] = breaker
        # 新建即上报初始状态：预热阶段 metrics 就能看到全部熔断器（而非等首次状态迁移）
        _set_gauge("llm_circuit_state", breaker.state_code, circuit=key)
        log.info("[llm_breaker] {} 已创建 state={}", key, breaker.state)
    return breaker


def circuit_snapshot() -> Dict[str, str]:
    """全部熔断器状态快照（管理/排障用）。"""
    return {k: b.state for k, b in _breakers.items()}


def log_breaker_snapshot(prefix: str = "") -> Dict[str, str]:
    """打印全部熔断器当前状态：一行聚合 + 逐条明细（启动完成/排障用）。"""
    snapshot = circuit_snapshot()
    head = f"[llm_breaker] {prefix}".rstrip()
    if not snapshot:
        log.info("{} 无已注册熔断器", head)
        return snapshot
    counts: Dict[str, int] = {}
    for state in snapshot.values():
        counts[state] = counts.get(state, 0) + 1
    brief = " / ".join(f"{s}×{n}" for s, n in sorted(counts.items()))
    log.info("{} 共 {} 个熔断器: {}", head, len(snapshot), brief)
    for key, state in snapshot.items():
        log.info("{} | {} = {}", head, key, state)
    return snapshot


# ── 内部工具 ───────────────────────────────────────────────────────────────

def _retry_after_seconds(exc: BaseException, cap: float) -> Optional[float]:
    """从 429 响应提取 Retry-After（秒），封顶 cap；取不到返回 None。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            raw = None
    if raw:
        try:
            return min(float(raw), cap)
        except (TypeError, ValueError):
            pass
    return None


def _backoff_delay(attempt: int, exc: Optional[BaseException] = None) -> float:
    """指数退避 + 抖动；429 带合法 Retry-After 时取两者较大值（封顶）。"""
    cfg = config.llm
    base = min(cfg.backoff_base_seconds * (2 ** attempt), cfg.backoff_max_seconds)
    ra = _retry_after_seconds(exc, cfg.backoff_max_seconds) if exc is not None else None
    delay = max(base, ra) if ra is not None else base
    return delay + random.uniform(0, 0.25 * base)


def _log_call(label: str, model: str, attempt: int, latency_s: float,
              outcome: str, fallback_used: bool = False) -> None:
    # 结构化调用日志：只记元数据，不记消息内容
    log.debug(
        "[llm_call] label={} model={} attempt={} outcome={} fallback={} latency={:.2f}s",
        label, model, attempt, outcome, fallback_used, latency_s,
    )


def _exec_kwargs(tools=None, max_tokens=None, temperature=None) -> Dict[str, Any]:
    """只透传显式给出的参数，其余交给 AsyncLLMClient 的默认值。"""
    kwargs: Dict[str, Any] = {}
    if tools is not None:
        kwargs["tools"] = tools
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


# ── 非流式执行 ─────────────────────────────────────────────────────────────

async def resilient_invoke(
    messages: List[Dict[str, Any]],
    *,
    chain: Sequence[str],
    label: str = "",
    tools: Optional[List[Dict]] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict[Any, Any]:
    """非流式调用：同模型退避重试 → 逐级 fallback（每级独立熔断）。

    chain：组件初始化时从自身配置解析出的模型链 [主模型, fallback...]；
    label：观测标签（组件名），仅用于指标/日志，不参与路由。
    """
    chain = [m for m in chain if str(m or "").strip()]
    kwargs = _exec_kwargs(tools, max_tokens, temperature)
    last_exc: Optional[BaseException] = None
    max_attempts = max(0, config.llm.max_attempts)

    for idx, model in enumerate(chain):
        fallback_used = idx > 0
        if fallback_used:
            _inc("llm_fallback_total", scenario=label, model=model)
        breaker = get_breaker(model)
        if not await breaker.acquire():
            log.warning("[llm] {} 熔断打开，跳过模型 {}（label={}）", chain[0], model, label)
            if last_exc is None:
                last_exc = LLMNoModelAvailableError(f"circuit open: {model}")
            continue

        for attempt in range(max_attempts + 1):
            try:
                t0 = time.monotonic()
                # llm span 直埋（观测）：每次实际 API 调用一个 span，失败自动置 error
                async with _llm_span(model, messages) as span:
                    llm_response, usage = await get_llm(model).invoke(messages, **kwargs)
                    prompt, completion = _usage_ints(usage)
                    span.add_attributes({
                        "model": model,
                        "finish_reason": str(llm_response.get("finish_reason") or ""),
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                    })
                    span.set_output({
                        "content": llm_response.get("content") or "",
                        "tool_calls": llm_response.get("tool_calls") or [],
                    })
                await breaker.record_success()
                _record_usage(model, usage)
                _record_call(label, model, None, (time.monotonic() - t0) * 1000, 0.0)
                _log_call(label, model, attempt, time.monotonic() - t0, "ok", fallback_used)
                return llm_response
            except asyncio.CancelledError:
                await breaker.release()  # 取消：归还半开探测名额
                raise  # 停止请求/任务取消：绝不重试
            except Exception as exc:
                cls = classify_llm_error(exc)
                _record_call(label, model, cls)
                _log_call(label, model, attempt, time.monotonic() - t0,
                          f"error_{cls.value}", fallback_used)
                last_exc = exc
                # 半开探测无论错误类型都要收敛；闭态只有上游健康类失败才计熔断
                await breaker.record_failure(count_as_failure=(cls is LLMErrorClass.RETRYABLE))
                if cls is LLMErrorClass.LOGIC:
                    raise  # 本地 bug：立即暴露
                if cls is not LLMErrorClass.RETRYABLE:
                    break  # OVERFLOW/FATAL：不重试，下一级模型
                if attempt >= max_attempts:
                    break  # 重试耗尽：下一级模型
                delay = _backoff_delay(attempt, exc)
                log.warning(
                    "[llm] label={} model={} 瞬态错误({})，{:.1f}s 后重试（{}/{}）: {}",
                    label, model, cls.value, delay, attempt + 1, max_attempts, exc,
                )
                _inc("llm_retry_total", scenario=label, model=model)
                await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise LLMNoModelAvailableError(f"label={label} 无可用模型（链为空）")


# ── 流式执行 ───────────────────────────────────────────────────────────────

async def resilient_invoke_stream(
    messages: List[Dict[str, Any]],
    *,
    chain: Sequence[str],
    label: str = "",
    tools: Optional[List[Dict]] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: Optional[float] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """流式调用：重试/换模型仅限**首 chunk 之前**；首 chunk 后失败直接抛。

    一旦有内容已透传给上层消费者，任何失败都不能透明重放，只能让本轮显式失败
    （否则用户会看到重复内容）。chain/label 语义同 resilient_invoke。
    """
    chain = [m for m in chain if str(m or "").strip()]
    kwargs = _exec_kwargs(tools, max_tokens, temperature)
    if timeout is not None:
        kwargs["timeout"] = timeout
    last_exc: Optional[BaseException] = None
    max_attempts = max(0, config.llm.max_attempts)

    for idx, model in enumerate(chain):
        fallback_used = idx > 0
        if fallback_used:
            _inc("llm_fallback_total", scenario=label, model=model)
        breaker = get_breaker(model)
        if not await breaker.acquire():
            log.warning("[llm] {} 熔断打开，跳过模型 {}（label={}）", chain[0], model, label)
            if last_exc is None:
                last_exc = LLMNoModelAvailableError(f"circuit open: {model}")
            continue

        first_chunk_seen = False
        for attempt in range(max_attempts + 1):
            t0 = time.monotonic()
            first_token_ms = 0.0
            usage = None
            try:
                # llm span 直埋（观测）：首 token 时点与 usage 随流补写
                content_parts: list[str] = []
                tool_calls_meta: list[Any] = []
                async with _llm_span(model, messages) as span:
                    async for item in get_llm(model).invoke_stream(messages, **kwargs):
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            first_token_ms = (time.monotonic() - t0) * 1000
                        if item.get("type") == "content":
                            content_parts.append(str(item.get("data") or ""))
                        elif item.get("type") == "tool_calls":
                            tool_calls_meta = item.get("data") or []
                        if item.get("type") == "response_info":
                            # 末尾 response_info 携带 usage：token 计数唯一喂入点
                            info = item.get("data") or {}
                            usage = info.get("usage")
                            _record_usage(model, usage)
                            prompt, completion = _usage_ints(usage)
                            span.add_attributes({
                                "model": str(info.get("model") or model),
                                "finish_reason": str(info.get("finish_reason") or ""),
                                "prompt_tokens": prompt,
                                "completion_tokens": completion,
                            })
                        yield item
                    span.set_output({"content": "".join(content_parts), "tool_calls": tool_calls_meta})
                await breaker.record_success()
                _record_call(label, model, None, (time.monotonic() - t0) * 1000, first_token_ms)
                _log_call(label, model, attempt, time.monotonic() - t0, "ok", fallback_used)
                return
            except asyncio.CancelledError:
                await breaker.release()  # 取消：归还半开探测名额
                raise  # 用户停止/客户端断开：绝不重试、不计失败
            except Exception as exc:
                cls = classify_llm_error(exc)
                _record_call(label, model, cls)
                _log_call(label, model, attempt, time.monotonic() - t0,
                          f"error_{cls.value}", fallback_used)
                last_exc = exc
                # 半开探测无论错误类型都要收敛；闭态只有上游健康类失败才计熔断
                await breaker.record_failure(count_as_failure=(cls is LLMErrorClass.RETRYABLE))
                if first_chunk_seen:
                    # 首 chunk 已透传：只能显式失败（透明重放会让用户看到重复内容）
                    log.warning("[llm] label={} 首 chunk 后失败({})，终止本轮: {}",
                                label, cls.value, exc)
                    raise
                if cls is LLMErrorClass.LOGIC:
                    raise
                if cls is not LLMErrorClass.RETRYABLE:
                    break
                if attempt >= max_attempts:
                    break
                delay = _backoff_delay(attempt, exc)
                log.warning(
                    "[llm] label={} model={} 流式瞬态错误({})，{:.1f}s 后重试（{}/{}）: {}",
                    label, model, cls.value, delay, attempt + 1, max_attempts, exc,
                )
                _inc("llm_retry_total", scenario=label, model=model)
                await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise LLMNoModelAvailableError(f"label={label} 无可用模型（链为空）")
