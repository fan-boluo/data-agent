"""Trace 上下文（contextvars）：turn 绑定 + 当前 parent span 栈 + 当前 agent 名。

机制前提（turn 生命周期函数在请求任务内直调）：
- 同任务内 ContextVar.set() 的 token 向调用方传播 → turn 绑定在 turn_start 设置，
  主动 span API / LLM 韧性层在任意嵌套层读取
- gather 并行任务继承 context 副本，任务内 set 不污染外层

消费方：api.start_span（parent 解析 + agent 名维护）/ LLM 韧性层（llm span 归属 agent）。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnBinding:
    """当前 turn 的 trace 身份（turn_start 设定，turn_end 清除）。"""

    session_id: str
    trace_id: str
    turn_id: str
    turn_span_id: str
    inline_input: bool = True  # llm span input 是否内联：在线问答 False（chat_messages 为真相源，存引用）；后台合成消息 True


_turn_binding: ContextVar[TurnBinding | None] = ContextVar("trace_turn_binding", default=None)
_current_parent: ContextVar[str | None] = ContextVar("trace_current_parent", default=None)
# 当前 agent 名（agent span 进入时设定，llm span 归属用；无 agent 域时为 None）
_current_agent: ContextVar[str | None] = ContextVar("trace_current_agent", default=None)


def get_turn_binding() -> TurnBinding | None:
    return _turn_binding.get()


def get_current_parent() -> str | None:
    """当前 span 栈顶；无主动 span 时由调用方回退到 turn span。"""
    return _current_parent.get()


def get_current_agent() -> str | None:
    """当前 agent 域名（llm span 的 name/agent 归属）；无 agent 域返回 None。"""
    return _current_agent.get()


def set_turn_binding(binding: TurnBinding | None) -> Token | None:
    """turn_start 设定绑定并清 parent 栈；turn_end 传 None 复位。返回 token 供恢复。"""
    if binding is None:
        return None
    return _turn_binding.set(binding)


def reset_turn_binding(token: Token | None) -> None:
    if token is not None:
        _safe_reset(_turn_binding, token)


def push_parent(span_id: str) -> Token:
    """进入 span：设为当前 parent（退出时 reset 回上一个，兄弟 span 不串联）。"""
    return _current_parent.set(span_id)


def pop_parent(token: Token) -> None:
    _safe_reset(_current_parent, token)


def push_agent(name: str) -> Token:
    """进入 agent span：记录当前 agent 名（llm span 归属）。"""
    return _current_agent.set(name)


def pop_agent(token: Token) -> None:
    _safe_reset(_current_agent, token)


def _safe_reset(var: ContextVar, token: Token) -> None:
    """跨 Context 复位兜底：异步生成器被其他任务/GC 收尾时 token 属于另一个 Context，
    reset 会抛 ValueError（"was created in a different Context"）。埋点复位失败不应
    影响业务，更不该被上层当作 LLM 失败统计。"""
    try:
        var.reset(token)
    except (ValueError, RuntimeError):
        pass
