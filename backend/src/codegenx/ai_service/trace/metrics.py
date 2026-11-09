"""Prometheus 指标定义与喂入 helper（trace 模块唯一指标出口）。

- 指标模块级单例定义一次；trace 生命周期函数与 LLM 韧性层/tool_executor/memory 直接引用
- LLM token 计数（prompt/completion）唯一喂入点在 LLM 韧性层（resilience._record_usage，
  app_id 按上下文归因）；调用计数（含失败分类，trigger=触发来源）由韧性层每次
  attempt 喂 feed_llm_attempt，延迟/首 token 仅成功收口时 observe
- 告警评估由 Prometheus alert rules 承接（infra/monitoring/prometheus_alerts.yml），
  本模块只维护需要跨事件状态的 streak gauge
"""
from __future__ import annotations

import time
from threading import Lock

from prometheus_client import Counter, Gauge, Histogram

from codegenx.ai_service.tools.base import ToolResult
from shared import log

# ---------------------------------------------------------------------------
# Session 级
# ---------------------------------------------------------------------------
sessions_total = Counter(
    "codegenx_sessions_total",
    "Total agent sessions created",
)

# 当前运行中的会话数：全局一个值（不按 app 拆分，看板直接展示当前值）
active_sessions = Gauge(
    "codegenx_active_sessions",
    "Currently running agent sessions",
)

# ---------------------------------------------------------------------------
# Turn 级
# ---------------------------------------------------------------------------
turns_total = Counter(
    "codegenx_turns_total",
    "Total turns across all sessions",
    ["app_id", "status"],
)

turn_duration_seconds = Histogram(
    "codegenx_turn_duration_seconds",
    "Turn wall-clock duration",
    ["app_id"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
llm_calls_total = Counter(
    "codegenx_llm_calls_total",
    "Total LLM call attempts（trigger=触发来源：agent 名或后台组件名）",
    ["app_id", "model", "trigger", "outcome"],
)

llm_latency_seconds = Histogram(
    "codegenx_llm_latency_seconds",
    "LLM call round-trip latency",
    ["model"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
)

llm_first_token_seconds = Histogram(
    "codegenx_llm_first_token_seconds",
    "Time-to-first-token for LLM calls",
    ["model"],
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10],
)

llm_prompt_tokens_total = Counter(
    "codegenx_llm_prompt_tokens_total",
    "Total prompt tokens sent to LLMs",
    ["app_id", "model"],
)

llm_completion_tokens_total = Counter(
    "codegenx_llm_completion_tokens_total",
    "Total completion tokens received from LLMs",
    ["app_id", "model"],
)

# ---------------------------------------------------------------------------
# LLM 韧性层（resilience.py 喂入：重试/降级/熔断 —— 全部"当前状态"口径，无历史累积）
# ---------------------------------------------------------------------------
llm_retrying = Gauge(
    "codegenx_llm_retrying",
    "Calls currently in backoff retry sleep (per model)",
    ["model"],
)
llm_last_retries = Gauge(
    "codegenx_llm_last_retries",
    "Retry attempts used by the most recent call on this model (0 = first try ok)",
    ["model"],
)
llm_last_fallback_seconds = Gauge(
    "codegenx_llm_last_fallback_seconds",
    "Unix ts of the most recent fallback switch (model=主模型 target=实际降级到的模型)",
    ["model", "target"],
)
llm_last_failure_seconds = Gauge(
    "codegenx_llm_last_failure_seconds",
    "Unix ts of the most recent failed attempt (outcome=错误分类)",
    ["model", "outcome"],
)
# 最近失败的归因：trace_id + 分类（值恒 1；每模型只保留一条，换 trace 时移除旧序列）
llm_last_failure_info = Gauge(
    "codegenx_llm_last_failure_info",
    "Latest LLM failure attribution: trace_id + error_class (value always 1)",
    ["model", "error_class", "trace_id"],
)
llm_circuit_state = Gauge(
    "codegenx_llm_circuit_state",
    "Circuit breaker state (0=closed 1=half_open 2=open)",
    ["circuit"],
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
# 调用量/成功率不在监控侧统计：前端按 telemetry.tool_semantics 出「每工具每日折线+表」
# dispatch_agent 不采集延迟：其耗时=子 Agent 全流程（数十秒），会把真实工具 P95 掩盖
_LATENCY_EXCLUDED_TOOLS = {"dispatch_agent"}

tool_latency_seconds = Histogram(
    "codegenx_tool_latency_seconds",
    "Tool execution latency",
    ["app_id", "tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120],
)

# 工具级熔断（P0-2）：与模型级同构的三态状态机，按工具/数据源独立计数
tool_circuit_state = Gauge(
    "codegenx_tool_circuit_state",
    "Tool circuit breaker state (0=closed 1=half_open 2=open)",
    ["tool_name"],
)

# 连续失败起点时间戳：now - 该值 = 已连续失败多久（成功即清零）
tool_failure_streak_since_seconds = Gauge(
    "codegenx_tool_failure_streak_since_seconds",
    "Unix ts of the first failure in the current consecutive-failure streak (0=no streak)",
    ["tool_name"],
)

# 本轮连续失败中最近一次失败的时间戳（成功即清零，仅在有 streak 时有意义）
tool_failure_last_seconds = Gauge(
    "codegenx_tool_failure_last_seconds",
    "Unix ts of the latest failure in the current consecutive-failure streak (0=no streak)",
    ["tool_name"],
)

# 最近一次失败的归因：trace_id（反查 telemetry.trace_spans 的入参/异常）+ 失败分类
# 基数由 remove() 控制：每个工具只保留当前这一条，避免序列累积
tool_last_failure_info = Gauge(
    "codegenx_tool_last_failure_info",
    "Latest tool failure attribution (value always 1)",
    ["tool_name", "trace_id", "error_class"],
)

# ---------------------------------------------------------------------------
# Memory（memory/metrics.py 喂入）——只保留两类：当前状态 Gauge（可告警）+ 质量
# 趋势 Counter；任务完成明细以 memory_task / memory_sync_checkpoint 表为真源，
# 纯描述性事件计数（触发原因/拒绝原因/对账修复数/归档数）不进指标，走日志与库。
# ---------------------------------------------------------------------------
# 提炼任务结果趋势（status: ok/failed/dead）——随时间看错误率，反映提炼质量
memory_extract_total = Counter(
    "codegenx_memory_extract_total",
    "warm_extract task outcomes",
    ["app_id", "status"],
)
# 任务队列水位（patrol 每 60s 刷新）：pending 积压=该提炼没提炼
memory_task_pending = Gauge(
    "codegenx_memory_task_pending",
    "memory_task rows in status='pending'",
)
memory_task_running = Gauge(
    "codegenx_memory_task_running",
    "memory_task rows in status='running'",
)
memory_task_dead = Gauge(
    "codegenx_memory_task_dead",
    "memory_task rows in status='dead' (retries exhausted)",
)
# 向量同步积压已随 pgvector 迁移下线：向量与记忆行同表同事务，无待同步队列。
# 一致性检验新鲜度/抽检未命中同样下线：单表无「双源分叉」，无需对账。
# hot 层注入水位（最近一次采样的用户维度值）
memory_hot_token_usage_ratio = Gauge(
    "codegenx_memory_hot_token_usage_ratio",
    "hot injection tokens / budget (latest user sampled)",
)
# 降级事件（罕见且关键，按 increase(...[15m]) 告警）
memory_degrade_total = Counter(
    "codegenx_memory_degrade_total",
    "Memory subsystem degradations",
    ["kind"],
)

# ---------------------------------------------------------------------------
# Embedding（embedding.py 喂入；熔断与 LLM/工具级同构，fast-fail 由调用方降级承接）
# ---------------------------------------------------------------------------
embedding_calls_total = Counter(
    "codegenx_embedding_calls_total",
    "Embedding batch calls by outcome",
    ["model", "outcome"],
)
embedding_latency_seconds = Histogram(
    "codegenx_embedding_latency_seconds",
    "Embedding batch round-trip latency (retry included)",
    ["model"],
    buckets=[0.05, 0.1, 0.3, 0.5, 1, 2, 5, 10, 30],
)
embedding_last_failure_seconds = Gauge(
    "codegenx_embedding_last_failure_seconds",
    "Unix ts of the most recent embedding batch failure (timeout/rate_limit/auth/connection/other)",
    ["model", "error_class"],
)
# 最近失败的归因：trace_id + 分类（值恒 1；每模型只保留一条，换 trace 时移除旧序列）
embedding_last_failure_info = Gauge(
    "codegenx_embedding_last_failure_info",
    "Latest embedding failure attribution: trace_id + error_class (value always 1)",
    ["model", "error_class", "trace_id"],
)
# embedding 级熔断（通用 CircuitBreaker 以 metric_prefix 反射引用）
embedding_circuit_state = Gauge(
    "codegenx_embedding_circuit_state",
    "Embedding circuit breaker state (0=closed 1=half_open 2=open)",
    ["circuit"],
)


def classify_embedding_error(exc: Exception) -> str:
    """embedding 失败分类：timeout / rate_limit / auth / connection / other。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in text:
        return "timeout"
    if "ratelimit" in name or "rate limit" in text or "429" in text:
        return "rate_limit"
    if (
        "authentication" in name or "permission" in name or "apikey" in name
        or "api key" in text or "401" in text or "403" in text
    ):
        return "auth"
    if "connect" in name or "connect" in text or "network" in text:
        return "connection"
    return "other"


def feed_embedding_call(
    *, model: str, latency_s: float, error_class: str = "",
) -> None:
    """单批 embedding 收口：成功计数+延迟；失败计数+最近失败时间戳（按错误分类）。"""
    if error_class:
        embedding_calls_total.labels(model=model, outcome="error").inc()
        embedding_last_failure_seconds.labels(model=model, error_class=error_class).set(time.time())
        _set_failure_attr(embedding_last_failure_info, _embedding_failure_attr, model, error_class)
        return
    embedding_calls_total.labels(model=model, outcome="ok").inc()
    embedding_latency_seconds.labels(model=model).observe(max(0.0, latency_s))


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
errors_total = Counter(
    "codegenx_errors_total",
    "Total errors across all scopes",
    ["app_id", "scope", "error_type"],
)

# ---------------------------------------------------------------------------
# 告警 streak gauge（需跨事件状态，由 _StreakTracker 维护）
# ---------------------------------------------------------------------------
tool_failure_streak = Gauge(
    "codegenx_tool_failure_streak",
    "Consecutive tool failure count (0 = no active streak)",
    ["tool_name"],
)

context_breach_streak = Gauge(
    "codegenx_context_breach_streak",
    "Max consecutive context breaches without compression across sessions (0 = none)",
)

llm_last_call_latency_seconds_gauge = Gauge(
    "codegenx_llm_last_call_latency_seconds",
    "Latency of the most recent LLM call",
    ["model"],
)

CONTEXT_USAGE_BREACH_RATIO = 0.85  # token_usage > 85% 预算且未压缩 → breach


class StreakTracker:
    """进程级 streak 状态（threading.Lock 保护），gauge 直推 Prometheus。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tool_failure: dict[str, int] = {}      # tool_name → streak（全局，跨会话）
        self._tool_failure_since: dict[str, float] = {}   # tool_name → 本轮 streak 起点 ts
        self._tool_failure_last: dict[str, float] = {}    # tool_name → 本轮最近失败 ts
        self._tool_failure_info: dict[str, tuple[str, str]] = {}  # tool_name → (trace_id, error_class)
        self._context_breach: dict[str, int] = {}    # session_id → streak

    def track_tool_outcome(self, *, session_id: str, tool_name: str, is_error: bool,
                           trace_id: str = "", error_class: str = "") -> None:
        """同一工具全局连续失败 streak（告警规则 ToolConsecutiveFailures 消费）。

        按工具计数、跨会话：该工具任一次成功即清零；session_id 仅用于日志定位。
        同时维护本轮连续失败的起点/最近失败时间与最近一次失败的 trace_id+分类，
        让「工具当前连续失败」一个面板就能定位到具体某次请求。
        """
        now = time.time()
        old_info: tuple[str, str] | None = None
        with self._lock:
            if is_error:
                streak = self._tool_failure.get(tool_name, 0) + 1
                self._tool_failure[tool_name] = streak
                # 起点只在 streak 从 0→1 时记录，后续失败沿用，成功清零
                since = self._tool_failure_since.get(tool_name) or now
                self._tool_failure_since[tool_name] = since
                self._tool_failure_last[tool_name] = now
                old_info = self._tool_failure_info.get(tool_name)
                if error_class == "circuit_open" and old_info:
                    # 熔断快速失败只累加 streak，不覆盖真实归因（保留导致熔断的那次 trace/分类）
                    new_info = old_info
                else:
                    new_info = (trace_id or "unknown", error_class or "unknown")
                    self._tool_failure_info[tool_name] = new_info
            else:
                streak = 0
                since = 0.0
                self._tool_failure[tool_name] = 0
                self._tool_failure_since.pop(tool_name, None)
                self._tool_failure_last.pop(tool_name, None)
                old_info = self._tool_failure_info.pop(tool_name, None)
                new_info = None

        tool_failure_streak.labels(tool_name=tool_name).set(streak)
        tool_failure_streak_since_seconds.labels(tool_name=tool_name).set(since)
        tool_failure_last_seconds.labels(tool_name=tool_name).set(
            self._tool_failure_last.get(tool_name, 0.0)
        )
        # info 序列只保留当前这一条：归因变化或恢复成功时移除旧 label，避免序列累积
        if old_info and old_info != new_info:
            try:
                tool_last_failure_info.remove(tool_name, *old_info)
            except KeyError:
                pass
        if new_info:
            tool_last_failure_info.labels(
                tool_name=tool_name, trace_id=new_info[0], error_class=new_info[1],
            ).set(1)

        if streak >= 3:
            log.warning("Tool failure streak={} tool={} session={} trace_id={} since={}",
                        streak, tool_name, session_id, new_info[0] if new_info else "-", int(since))

    def track_context_breach(self, *, session_id: str, token_usage: float, is_compress: bool) -> None:
        """上下文连续超阈值未压缩 streak（report_context_usage 每 step 喂入）。

        内部按会话计数；gauge 上报跨会话最大值——不带 session_id label，
        避免 Prometheus 序列随会话数无限膨胀（会话明细见 streak>=3 日志）。
        """
        breached = (token_usage > CONTEXT_USAGE_BREACH_RATIO) and not is_compress
        with self._lock:
            streak = self._context_breach.get(session_id, 0) + 1 if breached else 0
            self._context_breach[session_id] = streak
            context_breach_streak.set(max(self._context_breach.values(), default=0))
        if streak >= 3:
            log.warning("Context breach streak={} session={} usage={:.2f}", streak, session_id, token_usage)

    def track_llm_latency(self, *, model: str, latency_s: float) -> None:
        """最近一次 LLM 调用延迟 gauge（告警规则 LLMSingleTimeout 消费）。"""
        llm_last_call_latency_seconds_gauge.labels(model=model).set(latency_s)

    def cleanup_session(self, session_id: str) -> None:
        """会话结束清理 streak 状态（上下文 gauge 回落至剩余会话最大值）。"""
        with self._lock:
            self._context_breach.pop(session_id, None)
            context_breach_streak.set(max(self._context_breach.values(), default=0))


_tracker: StreakTracker | None = None
_tracker_lock = Lock()
# 最近失败归因的序列清理：model → (error_class, trace_id)
_failure_attr_lock = Lock()
_llm_failure_attr: dict[str, tuple[str, str]] = {}
_embedding_failure_attr: dict[str, tuple[str, str]] = {}


def get_streak_tracker() -> StreakTracker:
    global _tracker
    if _tracker is not None:
        return _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = StreakTracker()
    return _tracker


# ---------------------------------------------------------------------------
# recorder 喂入 helper（载荷来自 hook ctx，非 telemetry 对象）
# ---------------------------------------------------------------------------

def feed_session_start() -> None:
    """session 建立：总数 +1、活跃 +1（全局单值，不分 app）。"""
    sessions_total.inc()
    active_sessions.inc()


def feed_session_end() -> None:
    """session 结束：活跃 -1（时长不采集——会话由空闲超时关闭，时长无排障价值）。"""
    active_sessions.dec()


def feed_turn_end(*, app_id: str, status: str, duration_ms: float) -> None:
    """turn 收口：状态计数 + 时长直方图。"""
    turns_total.labels(app_id=app_id, status=status).inc()
    turn_duration_seconds.labels(app_id=app_id).observe(max(0.0, duration_ms) / 1000)


def feed_llm_attempt(
    *, app_id: str, model: str, trigger: str, outcome: str,
    total_ms: float = 0, first_token_ms: float = 0,
) -> None:
    """单次 LLM 调用 attempt：计数（含失败分类）+ 成功时延迟/首 token + streak。

    韧性层每次实际调用喂一次（成功+失败），trigger=触发来源（agent 名/组件名），
    outcome=ok 或 error_{类别}；token 计数不在此喂——由韧性层 _record_usage
    统一计数（app_id 按上下文归因），避免同一次调用双计数。
    """
    llm_calls_total.labels(app_id=app_id, model=model, trigger=trigger, outcome=outcome).inc()
    if outcome == "ok":
        llm_latency_seconds.labels(model=model).observe(max(0.0, total_ms) / 1000)
        if first_token_ms:
            llm_first_token_seconds.labels(model=model).observe(first_token_ms / 1000)
        get_streak_tracker().track_llm_latency(model=model, latency_s=total_ms / 1000)
    else:
        # 最近一次失败时间戳（结合熔断状态判断是否已恢复；outcome 去掉 error_ 前缀）
        cls = outcome.removeprefix("error_")
        llm_last_failure_seconds.labels(model=model, outcome=cls).set(time.time())
        _set_failure_attr(llm_last_failure_info, _llm_failure_attr, model, cls)


def _set_failure_attr(gauge: Gauge, store: dict[str, tuple[str, str]],
                      model: str, error_class: str) -> None:
    """记录"最近一次失败的 trace_id + 分类"。

    每模型只保留一条序列：换 trace 时先 remove 旧 label，否则每次失败都会新增时间序列。
    """
    new = (error_class or "unknown", _current_trace_id() or "unknown")
    with _failure_attr_lock:
        old = store.get(model)
        store[model] = new
    if old and old != new:
        try:
            gauge.remove(model, *old)
        except KeyError:
            pass
    gauge.labels(model=model, error_class=new[0], trace_id=new[1]).set(1)


def _current_trace_id() -> str:
    """当前请求的 trace_id（不在请求上下文时返回空串，不影响打点）。"""
    try:
        from codegenx.ai_service.trace.context import get_turn_binding

        binding = get_turn_binding()
        return str(getattr(binding, "trace_id", "") or "")
    except Exception:
        return ""


def feed_tool_call(*, app_id: str, session_id: str, tool_name: str, is_error: bool,
                   latency_ms: float, error_class: str = "") -> None:
    """单次工具调用：延迟 + 连续失败 streak（含起点/最近失败时间与 trace_id）。

    分类口径：arg_invalid=LLM 传入参数/SQL 有问题（业务失败，不计熔断）、
    execution=工具执行本身失败（连接/资源/驱动，计入熔断）、timeout=超时（计入熔断）、
    subagent_failed=dispatch_agent 子 Agent 未产出结果（不计熔断，归因到子 Agent 而非工具本身）。
    circuit_open 是熔断的结果而非失败原因，同样计入连续失败（该工具确实在快速失败）。
    """
    if tool_name not in _LATENCY_EXCLUDED_TOOLS:
        tool_latency_seconds.labels(app_id=app_id, tool_name=tool_name).observe(
            max(0.0, latency_ms) / 1000
        )
    get_streak_tracker().track_tool_outcome(
        session_id=session_id, tool_name=tool_name, is_error=is_error,
        trace_id=_current_trace_id(), error_class=error_class,
    )


def feed_error(*, app_id: str, scope: str, error_type: str = "") -> None:
    """请求执行异常计数。"""
    errors_total.labels(app_id=app_id, scope=scope, error_type=error_type).inc()