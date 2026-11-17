"""admin 监控 API 响应契约（前端零改动：字段与 camelCase 序列化沿用原 monitor schema）。

数据源已切换为 PG telemetry 派生（trace/query_service.py）；新增 turn span 树
（waterfall 前端组树用）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from shared.schema.common import CamelBaseModel, PageRequest


class MonitorToolCallDetail(CamelBaseModel):
    name: str = ""
    latency_ms: int = Field(default=0, alias="latencyMs")
    status: str = "success"
    call_count: int = Field(default=1, alias="callCount")


class MonitorSessionQueryRequest(PageRequest):
    status: str | None = None
    app_id: str | None = Field(default=None, alias="appId")
    user_id: str | None = Field(default=None, alias="userId")
    session_id: str | None = Field(default=None, alias="sessionId")
    trace_id: str | None = Field(default=None, alias="traceId")


class MonitorAlertQueryRequest(PageRequest):
    status: str | None = None
    level: str | None = None
    rule_name: str | None = Field(default=None, alias="ruleName")
    session_id: str | None = Field(default=None, alias="sessionId")


class MonitorSessionSummary(CamelBaseModel):
    session_id: str = Field(alias="sessionId")
    trace_id: str = Field(alias="traceId")
    app_id: str = Field(alias="appId")
    user_id: str = Field(default="", alias="userId")
    model: str = ""
    status: str = "running"
    total_turns: int = Field(default=0, alias="totalTurns")
    total_prompt_tokens: int = Field(default=0, alias="totalPromptTokens")
    total_completion_tokens: int = Field(default=0, alias="totalCompletionTokens")
    token_budget: int = Field(default=0, alias="tokenBudget")
    avg_llm_latency_ms: float = Field(default=0, alias="avgLlmLatencyMs")
    avg_first_token_ms: float = Field(default=0, alias="avgFirstTokenMs")
    max_llm_latency_ms: int = Field(default=0, alias="maxLlmLatencyMs")
    min_llm_latency_ms: int = Field(default=0, alias="minLlmLatencyMs")
    total_tool_calls: int = Field(default=0, alias="totalToolCalls")
    total_errors: int = Field(default=0, alias="totalErrors")
    recovery_count: int = Field(default=0, alias="recoveryCount")
    last_recovery_kind: str = Field(default="", alias="lastRecoveryKind")
    end_reason: str = Field(default="", alias="endReason")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")
    duration_ms: int = Field(default=0, alias="durationMs")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class MonitorTurnSummary(CamelBaseModel):
    trace_id: str = Field(alias="traceId")
    session_id: str = Field(alias="sessionId")
    request_id: str = Field(default="", alias="requestId")
    turn_id: str = Field(alias="turnId")
    turn_number: int = Field(default=0, alias="turnNumber")
    status: str = "running"
    prompt_tokens: int = Field(default=0, alias="promptTokens")
    completion_tokens: int = Field(default=0, alias="completionTokens")
    llm_latency_ms: int = Field(default=0, alias="llmLatencyMs")
    first_token_ms: int = Field(default=0, alias="firstTokenMs")
    llm_recovery_count: int = Field(default=0, alias="llmRecoveryCount")
    llm_recovery_kind: str = Field(default="", alias="llmRecoveryKind")
    tool_calls_count: int = Field(default=0, alias="toolCallsCount")
    tool_calls_detail: list[MonitorToolCallDetail] = Field(default_factory=list, alias="toolCallsDetail")
    memory_retrieval_ms: int = Field(default=0, alias="memoryRetrievalMs")
    context_tokens: int = Field(default=0, alias="contextTokens")
    context_token_usage: int = Field(default=0, alias="contextTokenUsage")
    error_count: int = Field(default=0, alias="errorCount")
    user_input: str = Field(default="", alias="userInput")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")
    duration_ms: int = Field(default=0, alias="durationMs")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class MonitorAlertRecordVO(CamelBaseModel):
    id: int | None = None
    rule_name: str = Field(alias="ruleName")
    level: str
    trace_id: str = Field(alias="traceId")
    session_id: str = Field(alias="sessionId")
    turn_id: str = Field(default="", alias="turnId")
    status: str = "open"
    message: str = ""
    observed_value: str = Field(default="", alias="observedValue")
    threshold_value: str = Field(default="", alias="thresholdValue")
    triggered_at: datetime | None = Field(default=None, alias="triggeredAt")
    resolved_at: datetime | None = Field(default=None, alias="resolvedAt")
    payload: dict[str, Any] = Field(default_factory=dict)


class MonitorOverviewStats(CamelBaseModel):
    total_sessions: int = Field(default=0, alias="totalSessions")
    running_sessions: int = Field(default=0, alias="runningSessions")
    success_sessions: int = Field(default=0, alias="successSessions")
    error_sessions: int = Field(default=0, alias="errorSessions")
    total_turns: int = Field(default=0, alias="totalTurns")
    avg_turn_duration_ms: float = Field(default=0, alias="avgTurnDurationMs")
    avg_llm_latency_ms: float = Field(default=0, alias="avgLlmLatencyMs")
    avg_first_token_ms: float = Field(default=0, alias="avgFirstTokenMs")
    avg_context_tokens: float = Field(default=0, alias="avgContextTokens")
    avg_context_token_usage: float = Field(default=0, alias="avgContextTokenUsage")
    total_tool_calls: int = Field(default=0, alias="totalToolCalls")
    total_tokens: int = Field(default=0, alias="totalTokens")
    open_alerts: int = Field(default=0, alias="openAlerts")


class MonitorSessionDetail(CamelBaseModel):
    session: MonitorSessionSummary
    turns: list[MonitorTurnSummary] = Field(default_factory=list)
    alerts: list[MonitorAlertRecordVO] = Field(default_factory=list)


class MonitorCleanupTableResult(CamelBaseModel):
    table_name: str = Field(alias="tableName")
    status: str = "success"
    affected_rows: int = Field(default=0, alias="affectedRows")
    cutoff_at: datetime | None = Field(default=None, alias="cutoffAt")
    error_message: str = Field(default="", alias="errorMessage")


class MonitorCleanupSummary(CamelBaseModel):
    retention_days: int = Field(alias="retentionDays")
    dry_run: bool = Field(default=False, alias="dryRun")
    status: str = "success"
    deleted_rows: int = Field(default=0, alias="deletedRows")
    executed_at: datetime | None = Field(default=None, alias="executedAt")
    table_results: list[MonitorCleanupTableResult] = Field(default_factory=list, alias="tableResults")


class MonitorToolDailyStat(CamelBaseModel):
    """单个工具在某一自然日的执行统计（Asia/Shanghai 口径）。"""

    tool_name: str = Field(alias="toolName")
    date: str = ""
    total: int = 0
    success: int = 0
    failure: int = 0


class MonitorToolDailyStats(CamelBaseModel):
    """近 N 天工具执行统计：日期轴 + 工具×日的明细。"""

    days: int = 7
    dates: list[str] = Field(default_factory=list)
    records: list[MonitorToolDailyStat] = Field(default_factory=list)


class MonitorLlmDailyStat(CamelBaseModel):
    """单个模型在某一自然日的调用统计（Asia/Shanghai 口径）。"""

    model_name: str = Field(alias="modelName")
    date: str = ""
    total: int = 0
    success: int = 0
    failure: int = 0


class MonitorLlmDailyStats(CamelBaseModel):
    """近 N 天模型调用统计：日期轴 + 模型×日的明细。"""

    days: int = 7
    dates: list[str] = Field(default_factory=list)
    records: list[MonitorLlmDailyStat] = Field(default_factory=list)


class TokenUsageQueryRequest(PageRequest):
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    user_id: str | None = Field(default=None, alias="userId")


class TokenUsageItem(CamelBaseModel):
    user_id: str = Field(alias="userId")
    model_name: str = Field(alias="modelName")
    total_prompt_tokens: int = Field(default=0, alias="totalPromptTokens")
    total_completion_tokens: int = Field(default=0, alias="totalCompletionTokens")
    total_tokens: int = Field(default=0, alias="totalTokens")
    ended_at: datetime | None = Field(default=None, alias="endedAt")


# ── 新增：turn span 树（waterfall） ─────────────────────────────────────────

class TurnSpanNode(CamelBaseModel):
    span_id: str = Field(alias="spanId")
    parent_span_id: str = Field(default="", alias="parentSpanId")
    op_type: str = Field(default="", alias="opType")
    name: str = ""
    agent: str = ""
    seq: int = 0
    status: str = "ok"
    error: str | None = None
    started_at: datetime | None = Field(default=None, alias="startedAt")
    duration_ms: float = Field(default=0, alias="durationMs")
    offset_ms: float = Field(default=0, alias="offsetMs")
    input: Any = None
    output: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TurnSpanTree(CamelBaseModel):
    trace_id: str = Field(alias="traceId")
    session_id: str = Field(alias="sessionId")
    turn_id: str = Field(default="", alias="turnId")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    total_duration_ms: float = Field(default=0, alias="totalDurationMs")
    span_count: int = Field(default=0, alias="spanCount")
    spans: list[TurnSpanNode] = Field(default_factory=list)
