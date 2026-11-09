"""trace 生命周期与 turn buffer：流程点直调（观测不寄生 hook 通道）。

内部 TurnBuffer 记录一次 turn 的全部 span，turn 结束时 flush 到 store，
store 入队累积达到 batch_size 后入库：
- traces 行 = 一轮请求：turn_start 建、turn_end flush；session 级聚合由 session_id 派生
- span 树：turn（根）→ agent → llm/tool/retrieval/compaction——llm span 由 LLM 韧性层
  直埋（resilience）、tool span 由 tool_executor 直埋，本模块不再做 before/after 配对
- turn 聚合（tokens/主力模型/工具数）在 turn_end 收口时从 spans 派生，单一来源
- 载荷截断：字符串单字段 payload_max_chars、rows_preview 限行、capture_content=false 只留元数据
- Prometheus 指标喂入（trace/metrics.py）与 telemetry 落库同源；llm 调用指标由韧性层喂
- 全部函数守卫式绝不抛错：trace 故障只降级不阻断主链路
"""
from __future__ import annotations

import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared import log

from codegenx.ai_service.utils.config import config

from . import context as trace_context
from . import metrics as trace_metrics
from .store import TraceStore


def _cfg():
    return config.trace


def _utc(ts: float | None = None) -> datetime:
    """时间戳 → UTC tz-aware datetime（asyncpg timestamptz 要求）。"""
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)


def _clip(obj: Any, max_chars: int) -> tuple[Any, bool]:
    """递归截断载荷中的字符串字段，返回 (对象, 是否发生截断)。"""
    if isinstance(obj, str):
        return (obj[:max_chars], True) if len(obj) > max_chars else (obj, False)
    if isinstance(obj, list):
        out, truncated = [], False
        for item in obj:
            clipped, t = _clip(item, max_chars)
            out.append(clipped)
            truncated = truncated or t
        return out, truncated
    if isinstance(obj, dict):
        out, truncated = {}, False
        for key, value in obj.items():
            clipped, t = _clip(value, max_chars)
            out[key] = clipped
            truncated = truncated or t
        return out, truncated
    return obj, False


@dataclass
class _TurnBuffer:
    """单 turn 内存 buffer：trace 行 + turn span + 已闭合 spans。"""

    trace_id: str
    session_id: str
    turn_id: str
    trace_row: dict[str, Any]
    turn_span: dict[str, Any]
    spans: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    errors: list[str] = field(default_factory=list)
    binding_token: Token | None = None  # contextvars turn 绑定 token（turn_end 复位）
    max_context_usage: float = 0.0    # report_context_usage 回写的最大上下文占用比


# session_id → {trace_id: buffer}；同会话请求串行，dispatch_agent 子代理共用同 buffer
_buffers: dict[str, dict[str, _TurnBuffer]] = {}
store = TraceStore(batch_size=_cfg().flush_batch_size)


# ---------------------------------------------------------------- 内部工具

def _current_buffer(session: Any) -> _TurnBuffer | None:
    """取会话当前 turn 的 buffer（trace_id 与 request_id 同源）。"""
    if session is None:
        return None
    return (_buffers.get(session.session_id) or {}).get(session.trace_id or session.request_id)


def _flush_turn(buffer: _TurnBuffer) -> None:
    """把一个 turn 的全部行入队（traces + turn span + 子 spans）。"""
    store.enqueue("traces", buffer.trace_row)
    store.enqueue("spans", buffer.turn_span)
    for span in buffer.spans:
        store.enqueue("spans", span)


def _derive_totals(buffer: _TurnBuffer) -> tuple[int, int, dict[str, int], int]:
    """turn 聚合从 spans 派生（单一来源：llm span attributes / op_type 计数）。

    返回 (prompt_tokens, completion_tokens, 模型调用计数, 工具调用数)。
    """
    total_prompt = total_completion = 0
    model_counts: dict[str, int] = {}
    tool_calls = 0
    for span in buffer.spans:
        op = span.get("op_type")
        if op == "llm":
            attrs = span.get("attributes") or {}
            total_prompt += int(attrs.get("prompt_tokens") or 0)
            total_completion += int(attrs.get("completion_tokens") or 0)
            model = str(attrs.get("model") or "unknown")
            model_counts[model] = model_counts.get(model, 0) + 1
        elif op in ("tool", "retrieval"):
            tool_calls += 1
    return total_prompt, total_completion, model_counts, tool_calls


# ---------------------------------------------------------------- turn/session 生命周期（流程点直调）

def turn_start(session: Any, turn: Any) -> None:
    """turn 开始：建 trace 行 + turn span + contextvars 绑定（auto_plan_manager 直调）。"""
    try:
        if not _cfg().enabled or session is None:
            return
        trace_id = session.trace_id or session.request_id
        if not trace_id:
            return
        started = float(getattr(turn, "started_at", 0) or time.time())
        trace_row = {
            "trace_id": trace_id,
            "session_id": session.session_id,
            "app_id": session.app_id,
            "user_id": session.user_id,
            "event": "qa",  # 根事件：问答轮（输入输出细节在 spans，问题全文在 chat_messages）
            "status": "running",
            "error": None,
            "total_tokens": 0,
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_ms": None,
            "started_at": _utc(started),
            "ended_at": None,
            "tags": {},
        }
        turn_span = {
            "span_id": uuid.uuid4().hex,
            "trace_id": trace_id,
            "parent_span_id": None,
            "session_id": session.session_id,
            "turn_id": session.request_id,
            "op_type": "turn",
            "name": "turn",
            "agent": session.agent_name,
            "seq": 0,
            "started_at": _utc(started),
            "duration_ms": None,
            "status": "ok",
            "error": None,
            "input": None,
            "output": None,
            "attributes": {},
        }
        _buffers.setdefault(session.session_id, {})[trace_id] = buffer = _TurnBuffer(
            trace_id=trace_id, session_id=session.session_id,
            turn_id=session.request_id, trace_row=trace_row, turn_span=turn_span,
        )
        # turn 绑定写入 contextvars：主动 span API / LLM 韧性层在嵌套层读取
        # inline_input=False：在线问答的 messages 以 chat_messages 为真相源，llm span 存引用防膨胀
        buffer.binding_token = trace_context.set_turn_binding(trace_context.TurnBinding(
            session_id=session.session_id, trace_id=trace_id,
            turn_id=session.request_id, turn_span_id=turn_span["span_id"],
            inline_input=False,
        ))
    except Exception as exc:  # noqa: BLE001 — trace 故障只降级
        log.error("trace turn_start 异常: {}", exc)


def turn_end(session: Any, turn: Any) -> None:
    """turn 收口：聚合派生 + trace 行定稿 + flush + 绑定复位（auto_plan_manager 直调）。"""
    try:
        if not _cfg().enabled or session is None:
            return
        trace_id = session.trace_id or session.request_id
        buffer = (_buffers.get(session.session_id) or {}).pop(trace_id, None)
        if buffer is None:
            return
        # turn 聚合从 spans 派生（单一来源：llm span attributes / op_type 计数）
        total_prompt, total_completion, model_counts, tool_calls = _derive_totals(buffer)
        state = getattr(turn, "state", None)
        status = str(getattr(state, "value", state) or "completed").lower()
        finished = float(getattr(turn, "finished_at", 0) or time.time())
        started = float(getattr(turn, "started_at", 0) or finished)
        duration = round(max(0.0, finished - started) * 1000, 1)
        error = str(getattr(turn, "error_text", "") or "")
        # trace 行收口：状态、主力模型、tokens、耗时、聚合 tags
        # tags 合并保留外部注入（add_turn_tags：eval_set/case_id 等）
        buffer.trace_row.update(
            status=status,
            error=error or None,
            total_tokens=total_prompt + total_completion,
            model=max(model_counts, key=model_counts.get) if model_counts else "",
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            duration_ms=duration,
            ended_at=_utc(finished),
        )
        buffer.trace_row["tags"].update({
            "tool_calls": tool_calls,
            "context_usage": round(buffer.max_context_usage, 4),
        })
        buffer.turn_span.update(duration_ms=duration, status=status, error=error or None)
        if buffer.errors:
            buffer.turn_span["attributes"]["errors"] = buffer.errors
        _flush_turn(buffer)
        trace_context.reset_turn_binding(buffer.binding_token)
        # Prometheus 喂入：turn 状态计数 + 时长直方图
        trace_metrics.feed_turn_end(
            app_id=buffer.trace_row["app_id"], status=status, duration_ms=duration,
        )
    except Exception as exc:  # noqa: BLE001 — trace 故障只降级
        log.error("trace turn_end 异常: {}", exc)


def turn_error(session: Any, error: Any) -> None:
    """turn 执行异常：错误计数 + errors 收集（auto_plan_manager except 分支直调）。"""
    try:
        if not _cfg().enabled:
            return
        if session is not None:
            trace_metrics.feed_error(
                app_id=str(getattr(session, "app_id", "") or "main"), scope="turn",
            )
        buffer = _current_buffer(session)
        if buffer is None:
            return
        if isinstance(error, (list, tuple)):
            error = "; ".join(str(e) for e in error)
        if error:
            buffer.errors.append(str(error)[:2000])
    except Exception as exc:  # noqa: BLE001 — trace 故障只降级
        log.error("trace turn_error 异常: {}", exc)


def session_start(session: Any) -> None:
    """session 建立：sessions_total + active_sessions（chat_service 直调）。"""
    try:
        if not _cfg().enabled or session is None:
            return
        trace_metrics.feed_session_start()
    except Exception as exc:  # noqa: BLE001 — trace 故障只降级
        log.error("trace session_start 异常: {}", exc)


async def session_end(session: Any) -> None:
    """session 关闭：active 回落 + 未收口 turn 兜底落库 + store flush。"""
    try:
        if not _cfg().enabled or session is None:
            return
        trace_metrics.feed_session_end()
        trace_metrics.get_streak_tracker().cleanup_session(session.session_id)
        # 兜底：未正常 turn_end 的轮次全量落库
        for buffer in (_buffers.pop(session.session_id, None) or {}).values():
            _flush_turn(buffer)
        await store.flush()
    except Exception as exc:  # noqa: BLE001 — trace 故障只降级
        log.error("trace session_end 异常: {}", exc)


# ---------------------------------------------------------------- 后台任务 root trace

def _close_background(buffer: _TurnBuffer, *, status: str) -> None:
    """后台 trace 收口：聚合派生 + 定稿 + flush + 绑定复位。"""
    finished = time.time()
    duration = round((finished - buffer.trace_row["started_at"].timestamp()) * 1000, 1)
    total_prompt, total_completion, model_counts, _tools = _derive_totals(buffer)
    buffer.trace_row.update(
        status=status,
        total_tokens=total_prompt + total_completion,
        model=max(model_counts, key=model_counts.get) if model_counts else "",
        prompt_tokens=total_prompt,
        completion_tokens=total_completion,
        duration_ms=duration,
        ended_at=_utc(finished),
    )
    buffer.turn_span.update(duration_ms=duration, status=status)
    (_buffers.get(buffer.session_id) or {}).pop(buffer.trace_id, None)
    _flush_turn(buffer)
    trace_context.reset_turn_binding(buffer.binding_token)


@asynccontextmanager
async def background_trace(trigger: str, *, session_id: str, app_id: str,
                           user_id: str = "", note: str = "",
                           trigger_event: str = "turn_end"):
    """后台任务独立 root trace（session_summary 等后台 LLM 调用的链路归属）。

    建合成 trace（event=trigger、tags.note 记说明）+ 根 span（op_type=触发事件
    turn_end/session_end——后台任务不是 turn）+ contextvars 绑定——块内 llm span/token
    指标自动归属（token 归因真实 app 而非 system）；正常退出收口 flush，业务异常
    透传且 trace 置 failed。守卫式绝不抛错。
    """
    buffer = None
    try:
        if not _cfg().enabled or not session_id:
            yield None
            return
        trace_id = uuid.uuid4().hex
        started = time.time()
        trace_row = {
            "trace_id": trace_id,
            "session_id": session_id,
            "app_id": app_id or "main",
            "user_id": user_id,
            "event": trigger,
            "status": "running",
            "error": None,
            "total_tokens": 0,
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_ms": None,
            "started_at": _utc(started),
            "ended_at": None,
            "tags": {"note": note} if note else {},
        }
        turn_span = {
            "span_id": uuid.uuid4().hex,
            "trace_id": trace_id,
            "parent_span_id": None,
            "session_id": session_id,
            "turn_id": None,
            "op_type": trigger_event,
            "name": trigger,
            "agent": "background",
            "seq": 0,
            "started_at": _utc(started),
            "duration_ms": None,
            "status": "ok",
            "error": None,
            "input": None,
            "output": None,
            "attributes": {},
        }
        _buffers.setdefault(session_id, {})[trace_id] = buffer = _TurnBuffer(
            trace_id=trace_id, session_id=session_id,
            turn_id="", trace_row=trace_row, turn_span=turn_span,
        )
        buffer.binding_token = trace_context.set_turn_binding(trace_context.TurnBinding(
            session_id=session_id, trace_id=trace_id,
            turn_id="", turn_span_id=turn_span["span_id"],
            inline_input=True,  # 后台任务 messages 为合成内容（无 DB 真相源），内联截断保留
        ))
        yield buffer
    finally:
        if buffer is not None:
            try:
                errored = sys.exc_info()[0] is not None
                _close_background(buffer, status="failed" if errored else "completed")
            except Exception as exc:  # noqa: BLE001 — trace 故障只降级
                log.error("trace background_trace 收口异常: {}", exc)
