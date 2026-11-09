"""主动 span API：压缩 / agent 边界等无 hook 事件的链路环节手动埋点。

主流 SDK 风格（OTel span / Langfuse observation）：上下文管理器，异常自动置 error。
- 无 turn 绑定（非请求域）或 trace.enabled=false → no-op 绝不抛错
- parent 解析：root=True 恒挂 turn span；否则 _current_parent（agent/tool scope）回退 turn span
- SpanHandle.discard()：无实际压缩等场景丢弃 span（不落库不入 buffer）
- report_context_usage：上下文占用每 step 上报（turn 聚合 max_context_usage +
  context_breach_streak 喂入），与 compaction span 解耦
- current_app_id：当前 turn 的 app_id（llm 客户端层 token 计数归因；后台/无绑定返回 system）
- 后台任务晚于 turn_end（buffer 已收口）→ span 直接 enqueue 落库不丢
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from shared import log

from . import context as trace_context
from . import metrics as trace_metrics
from .recorder import _buffers, _clip, _cfg, _utc, store


class SpanHandle:
    """span 句柄：with 块内可补写 output/attributes/status。"""

    __slots__ = ("span", "buffer", "_closed")

    def __init__(self, span: dict[str, Any] | None, buffer=None) -> None:
        self.span = span
        self.buffer = buffer
        self._closed = False

    def set_output(self, output: Any) -> None:
        if self.span is not None and not self._closed:
            if _cfg().capture_content:
                self.span["output"], _ = _clip(output, _cfg().payload_max_chars)
            else:
                self.span["output"] = {"truncated_meta": True}

    def add_attributes(self, attrs: dict[str, Any]) -> None:
        if self.span is not None and not self._closed and attrs:
            self.span["attributes"].update(attrs)

    def set_status(self, *, ok: bool, error: str | None = None) -> None:
        if self.span is not None and not self._closed:
            self.span["status"] = "ok" if ok else "error"
            self.span["error"] = (str(error)[:2000] if error else None) if not ok else None

    def discard(self) -> None:
        """丢弃 span（未发生实际压缩等场景：不落库、不入 buffer）。"""
        self.span = None

    def _close(self) -> None:
        """with 退出收口：时长定稿 + 入队（discard 后 span=None 直接跳过）。"""
        if self.span is None or self._closed:
            return
        self._closed = True
        span, buffer = self.span, self.buffer
        span["duration_ms"] = round((time.time() - span["_t0"]) * 1000, 1)
        del span["_t0"]
        # buffer 已被 turn_end 收口（后台任务晚归）→ 直接入队，否则等 turn flush
        live = (_buffers.get(buffer.session_id) or {}).get(buffer.trace_id)
        if live is buffer:
            buffer.spans.append(span)
        else:
            store.enqueue("spans", span)


_NOOP_SPAN = SpanHandle(None)


@asynccontextmanager
async def start_span(
    op_type: str, name: str, *,
    root: bool = False, attributes: dict[str, Any] | None = None, input_data: Any = None,
):
    """开启一个主动 span：agent / compaction 等主动埋点入口。"""
    binding = trace_context.get_turn_binding()
    if binding is None or not _cfg().enabled:
        yield _NOOP_SPAN
        return
    buffer = (_buffers.get(binding.session_id) or {}).get(binding.trace_id)
    if buffer is None:
        yield _NOOP_SPAN
        return
    parent = binding.turn_span_id if root else (
        trace_context.get_current_parent() or binding.turn_span_id
    )
    buffer.seq += 1
    if _cfg().capture_content and input_data is not None:
        clipped, _ = _clip(input_data, _cfg().payload_max_chars)
    else:
        clipped = None if input_data is None else {"truncated_meta": True}
    span = {
        "span_id": uuid.uuid4().hex,
        "trace_id": buffer.trace_id,
        "parent_span_id": parent,
        "session_id": buffer.session_id,
        "turn_id": buffer.turn_id,
        "op_type": op_type,
        "name": name,
        # agent 归属：统一继承当前 agent 域（tool/retrieval/compaction 也带），agent span 以自身名兜底
        "agent": trace_context.get_current_agent() or (name if op_type == "agent" else ""),
        "seq": buffer.seq,
        "started_at": _utc(),
        "duration_ms": None,
        "status": "ok",
        "error": None,
        "input": clipped,
        "output": None,
        "attributes": dict(attributes or {}),
        "_t0": time.time(),
    }
    handle = SpanHandle(span, buffer)
    token = trace_context.push_parent(span["span_id"])
    agent_token = trace_context.push_agent(name) if op_type == "agent" else None
    try:
        yield handle
    except (GeneratorExit, asyncio.CancelledError):
        # 用户停止/任务取消：链路中断，不算 span 失败（否则 stop 也会在 span 列表里报错）
        raise
    except BaseException as exc:  # 异常自动置 error 后原样上抛
        handle.set_status(ok=False, error=str(exc) or type(exc).__name__)
        raise
    finally:
        trace_context.pop_parent(token)
        if agent_token is not None:
            trace_context.pop_agent(agent_token)
        try:
            handle._close()
        except Exception as close_exc:  # 收口绝不影响业务
            log.error("span 收口异常: {}", close_exc)


def report_context_usage(context_usage: float, *, compressed: bool) -> None:
    """上下文占用上报（与 span 解耦，每 step 调用）：
    回写 turn 聚合 max_context_usage + context_breach_streak 喂入；
    compressed=摘要压缩生效时 streak 归零。
    """
    if not isinstance(context_usage, (int, float)):
        return
    binding = trace_context.get_turn_binding()
    if binding is None or not _cfg().enabled:
        return
    buffer = (_buffers.get(binding.session_id) or {}).get(binding.trace_id)
    if buffer is None:
        return
    buffer.max_context_usage = max(buffer.max_context_usage, float(context_usage))
    trace_metrics.get_streak_tracker().track_context_breach(
        session_id=buffer.session_id,
        token_usage=float(context_usage),
        is_compress=compressed,
    )


def current_app_id() -> str:
    """当前 turn 归属 app_id（llm 客户端层 token 计数归因）。

    有 turn 绑定且 buffer 存活 → trace_row.app_id（缺省 "main"）；
    无绑定/晚归（后台任务）→ "system"。
    """
    binding = trace_context.get_turn_binding()
    if binding is None:
        return "system"
    buffer = (_buffers.get(binding.session_id) or {}).get(binding.trace_id)
    if buffer is None:
        return "system"
    return str(buffer.trace_row.get("app_id") or "main")


def add_turn_tags(**tags: Any) -> None:
    """向当前 turn 的 trace_row.tags 合并键值（评测框架注入 eval_set/case_id 等）。"""
    binding = trace_context.get_turn_binding()
    if binding is None or not tags:
        return
    buffer = (_buffers.get(binding.session_id) or {}).get(binding.trace_id)
    if buffer is not None:
        buffer.trace_row["tags"].update(tags)
