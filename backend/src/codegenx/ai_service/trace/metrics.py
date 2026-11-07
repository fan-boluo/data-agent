"""Prometheus 指标定义与喂入 helper（trace 模块唯一指标出口）。

- 指标模块级单例定义一次；trace 生命周期函数与 LLM 韧性层/tool_executor/memory 直接引用
- LLM token 计数（prompt/completion）唯一喂入点在 LLM 韧性层（resilience._record_usage，
  app_id 按上下文归因）；调用计数（含失败分类，trigger=触发来源）由韧性层每次
  attempt 喂 feed_llm_attempt，延迟/首 token 仅成功收口时 observe
- 告警评估由 Prometheus alert rules 承接（infra/monitoring/prometheus_alerts.yml），
  本模块只维护需要跨事件状态的 streak gauge
"""
from __future__ import annotations

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
    ["app_id", "status"],
)

active_sessions = Gauge(
    "codegenx_active_sessions",
    "Currently running agent sessions",
    ["app_id"],
)

session_duration_seconds = Histogram(
    "codegenx_session_duration_seconds",
    "Session wall-clock duration",
    ["app_id"],
    buckets=[30, 60, 120, 300, 600, 900, 1800, 3600],
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
# LLM 韧性层（resilience.py 喂入：重试/降级/熔断）
# ---------------------------------------------------------------------------
llm_retry_total = Counter(
    "codegenx_llm_retry_total",
    "Same-model retries by the resilient executor",
    ["scenario", "model"],
)
llm_fallback_total = Counter(
    "codegenx_llm_fallback_total",
    "Calls that switched to a fallback model",
    ["scenario", "model"],
)
llm_breaker_opens_total = Counter(
    "codegenx_llm_breaker_opens_total",
    "Circuit breaker OPEN transitions",
    ["circuit"],
)
llm_circuit_state = Gauge(
    "codegenx_llm_circuit_state",
    "Circuit breaker state (0=closed 1=half_open 2=open)",
    ["circuit"],
)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
tool_calls_total = Counter(
    "codegenx_tool_calls_total",
    "Total tool invocations",
    ["app_id", "tool_name", "status"],
)

tool_latency_seconds = Histogram(
    "codegenx_tool_latency_seconds",
    "Tool execution latency",
    ["app_id", "tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
)

# 工具级熔断（P0-2）：与模型级同构的三态状态机，按工具/数据源独立计数
tool_circuit_state = Gauge(
    "codegenx_tool_circuit_state",
    "Tool circuit breaker state (0=closed 1=half_open 2=open)",
    ["tool_name"],
)

tool_breaker_opens_total = Counter(
    "codegenx_tool_breaker_opens_total",
    "Tool circuit breaker open events",
    ["tool_name"],
)

# 失败分类：exception / timeout / infra / business / circuit_open
tool_errors_total = Counter(
    "codegenx_tool_errors_total",
    "Tool failures by error class",
    ["tool_name", "error_class"],
)

# ---------------------------------------------------------------------------
# Memory（memory/metrics.py 喂入 warm_extract 任务指标）
# ---------------------------------------------------------------------------
memory_extract_total = Counter(
    "codegenx_memory_extract_total",
    "warm_extract task outcomes",
    ["app_id", "status"],
)
memory_extract_latency_seconds = Histogram(
    "codegenx_memory_extract_latency_seconds",
    "warm_extract task wall-clock duration",
    ["app_id"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)
memory_extract_triggered_total = Counter(
    "codegenx_memory_extract_triggered_total",
    "warm_extract enqueue by trigger reason",
    ["reason"],
)
memory_write_rejected_total = Counter(
    "codegenx_memory_write_rejected_total",
    "Candidate memories rejected at admission",
    ["reason"],
)
memory_task_dead = Gauge(
    "codegenx_memory_task_dead",
    "memory_task rows in status='dead'",
)
memory_task_stuck_reclaimed_total = Counter(
    "codegenx_memory_task_stuck_reclaimed_total",
    "Stuck running tasks reset to pending by patrol",
)

memory_vector_pending = Gauge(
    "codegenx_memory_vector_pending",
    "Rows with vector_synced_at IS NULL (layer=2)",
)
memory_vector_sync_fail_total = Counter(
    "codegenx_memory_vector_sync_fail_total",
    "Vector forward-sync batch failures",
)
memory_reconcile_missing_total = Counter(
    "codegenx_memory_reconcile_missing_total",
    "Reconcile: MySQL has / Qdrant missing, repaired",
)
memory_reconcile_ghost_total = Counter(
    "codegenx_memory_reconcile_ghost_total",
    "Reconcile: Qdrant ghosts cleaned",
)
memory_reconcile_spotcheck_miss_total = Counter(
    "codegenx_memory_reconcile_spotcheck_miss_total",
    "Spot-check samples not recalled by own summary",
)

memory_hot_token_usage_ratio = Gauge(
    "codegenx_memory_hot_token_usage_ratio",
    "hot injection tokens / budget (latest user sampled)",
)
memory_hot_count = Gauge(
    "codegenx_memory_hot_count",
    "Active hot entries for the latest sampled user",
)
memory_degrade_total = Counter(
    "codegenx_memory_degrade_total",
    "Memory subsystem degradations",
    ["kind"],
)

memory_conflict_detected_total = Counter(
    "codegenx_memory_conflict_detected_total",
    "Conflict pairs resolved by daily conflict_detect",
)
_merged_total = Counter(
    "codegenx_memory_consolidate_merged_total",
    "Duplicate warm entries merged by consolidate",
)
memory_archived_total = Counter(
    "codegenx_memory_archived_total",
    "warm entries soft-deleted / archived",
    ["action"],
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
embedding_batch_size = Histogram(
    "codegenx_embedding_batch_size",
    "Texts per embedding batch request",
    ["model"],
    buckets=[1, 2, 5, 10, 20, 50],
)
embedding_errors_total = Counter(
    "codegenx_embedding_errors_total",
    "Embedding failures by error class",
    ["model", "error_class"],
)
# embedding 级熔断（通用 CircuitBreaker 以 metric_prefix 反射引用）
embedding_breaker_opens_total = Counter(
    "codegenx_embedding_breaker_opens_total",
    "Embedding circuit breaker OPEN transitions",
    ["circuit"],
)
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
    *, model: str, batch_size: int, latency_s: float, error_class: str = "",
) -> None:
    """单批 embedding 收口：批次计数 + 批大小 + 成功延迟/失败分类。"""
    embedding_batch_size.labels(model=model).observe(max(0, batch_size))
    if error_class:
        embedding_calls_total.labels(model=model, outcome="error").inc()
        embedding_errors_total.labels(model=model, error_class=error_class).inc()
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
        self._context_breach: dict[str, int] = {}    # session_id → streak

    def track_tool_outcome(self, *, session_id: str, tool_name: str, is_error: bool) -> None:
        """同一工具全局连续失败 streak（告警规则 ToolConsecutiveFailures 消费）。

        按工具计数、跨会话：该工具任一次成功即清零；session_id 仅用于日志定位。
        """
        with self._lock:
            streak = self._tool_failure.get(tool_name, 0) + 1 if is_error else 0
            self._tool_failure[tool_name] = streak
        tool_failure_streak.labels(tool_name=tool_name).set(streak)
        if streak >= 3:
            log.warning("Tool failure streak={} tool={} session={}", streak, tool_name, session_id)

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

def feed_session_start(*, app_id: str, status: str = "running") -> None:
    """session 建立：总数 +1、活跃 +1。"""
    sessions_total.labels(app_id=app_id, status=status).inc()
    active_sessions.labels(app_id=app_id).inc()


def feed_session_end(*, app_id: str, duration_s: float) -> None:
    """session 结束：活跃 -1、时长直方图。"""
    active_sessions.labels(app_id=app_id).dec()
    session_duration_seconds.labels(app_id=app_id).observe(max(0.0, duration_s))


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


def feed_tool_call(*, app_id: str, session_id: str, tool_name: str, is_error: bool,
                   latency_ms: float, error_class: str = "") -> None:
    """单次工具调用：计数/延迟 + 连续失败 streak + 失败分类（异常统一上报）。"""
    status = "error" if is_error else "ok"
    tool_calls_total.labels(app_id=app_id, tool_name=tool_name, status=status).inc()
    tool_latency_seconds.labels(app_id=app_id, tool_name=tool_name).observe(max(0.0, latency_ms) / 1000)
    if is_error:
        tool_errors_total.labels(tool_name=tool_name, error_class=error_class or "unknown").inc()
    get_streak_tracker().track_tool_outcome(session_id=session_id, tool_name=tool_name, is_error=is_error)


def feed_error(*, app_id: str, scope: str, error_type: str = "") -> None:
    """请求执行异常计数。"""
    errors_total.labels(app_id=app_id, scope=scope, error_type=error_type).inc()