"""
提炼触发两级漏斗（P1-2，设计方案 v2.1 §4.1）。

第一级 · 轻量信号判断（每轮，成本极低，不调 LLM）：
  每轮结束后用规则+正则判断本轮是否含记忆信号（偏好/纠正/事实/资源指向），
  命中则累加 memory_watermark.pending_signals / pending_tokens。

第二级 · LLM 提炼（达阈值才触发）：
  - pending_signals >= signal_threshold(5)
  - pending_tokens  >= token_threshold(3000)
  - pending_signals >= 1 且距 last_extract_at 超过 idle_trigger_minutes(30)
  - 会话结束事件
  满足任一 → 投 warm_extract（dedup 防在途重复；水位推进后计数清零，
  uk_idempotency 预留给强幂等场景，在途去重靠 dedup 查询即可）。

与文档 §4.1 的偏差说明：文档里第一级同时推进 last_seq，本实现保持
last_seq 专属提炼 Worker（消费位点唯一写者），漏斗只累计信号量——
两个写者改同一水位会互相踩，信号量 + 消费位点分离更干净。

兜底扫描（进程崩溃补投）在 scheduler：scan_stale_pending 找有积压信号
且超时未处理的会话补投（见 schedule/memory.py）。
"""
from __future__ import annotations

import re
import time

from shared import log
from codegenx.ai_service.hook import HookContext, HookEvent, on


# ── 第一级：轻量信号正则（§4.1 四类）────────────────────────────────────────────
_SIGNAL_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in (
        r"我喜欢|我习惯|不要|别用|以后都|记住",                    # 偏好表达
        r"不对|应该是|我说的是|你搞错了|重新",                    # 纠正表达
        r"我是|我在做|我们项目|我的",                              # 事实陈述
        r"(?:[A-Za-z]:)?[\\/~][\w./\\-]{2,}|https?://\S+|\w+\.\w{1,4}\b",  # 资源指向：路径/URL/文件名
    )
]

# 每轮 token 估算口径与 chat_message.content_tokens 一致（len//4，无本地 tokenizer）


def detect_signals(text: str) -> int:
    """单条消息命中信号类数（同类多次命中计 1，避免长文本刷分）。"""
    if not text:
        return 0
    return sum(1 for p in _SIGNAL_PATTERNS if p.search(text))


def estimate_round_tokens(messages: list[dict]) -> int:
    """本轮 user+assistant 文本 token 估算（len//4 口径）。"""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):  # 多模态段拼接
            content = "; ".join(str(seg.get("content", "")) for seg in content if isinstance(seg, dict))
        total += len(str(content or "")) // 4
    return total


async def process_turn_signal(
    app_id: str,
    user_id: str,
    session_id: str,
    round_messages: list[dict],
    agent_name: str = "",
) -> None:
    """on_turn_end 调用：信号判断 → 累加水位 → 达阈值投递 warm_extract。

    round_messages: 本轮参与判断的消息（至少含最后一条 user 输入及其收尾
    assistant 回复）。全链路失败静默——漏斗丢一轮信号无正确性影响。
    agent_name：会话归属智能体（偏差①写门控：随任务下发供 scheduler 门控）。
    """
    try:
        cfg = config_memory_trigger()
        if not cfg.enabled:
            return
        # 信号数 = 各消息命中类数之和；tokens = 本轮全部文本估算
        signals = sum(detect_signals(_msg_text(m)) for m in round_messages)
        tokens = estimate_round_tokens(round_messages)
        if signals <= 0 and tokens <= 0:
            return

        from codegenx.ai_service.schedule.memory_task_store import get_memory_task_store
        store = get_memory_task_store()
        state = await store.bump_pending(
            session_id, app_id=str(app_id), user_id=str(user_id or ""),
            signals=signals, tokens=tokens,
        )
        if state is None:
            return
        pending_signals = int(state.get("pending_signals") or 0)
        pending_tokens = int(state.get("pending_tokens") or 0)
        last_extract_at = state.get("last_extract_at")

        # ── 第二级：双路阈值 / 空闲超时 ───────────────────────────────────────────
        reason = ""
        if pending_signals >= cfg.signal_threshold:
            reason = "signal"
        elif pending_tokens >= cfg.token_threshold:
            reason = "token"
        elif pending_signals >= 1 and _idle_expired(last_extract_at, cfg.idle_trigger_minutes):
            reason = "timeout"
        if not reason:
            return

        enqueued = await store.enqueue(
            "warm_extract",
            app_id=str(app_id),
            session_id=session_id,
            user_id=str(user_id or ""),
            payload={"agent_name": agent_name} if agent_name else None,
            dedup=True,
        )
        if enqueued is not None:
            log.debug(
                "[funnel] 会话 {} 触发提炼（{}）: signals={} tokens={}",
                session_id, reason, pending_signals, pending_tokens,
            )
    except Exception as exc:  # noqa: BLE001 — 漏斗失败不影响对话
        log.debug("[funnel] 信号处理失败（非致命）: {}", exc)


async def process_session_end(
    app_id: str, user_id: str, session_id: str, agent_name: str = ""
) -> None:
    """on_session_end 调用：会话结束事件直接投递（§4.1 触发条件之一）。"""
    try:
        from codegenx.ai_service.schedule.memory_task_store import get_memory_task_store
        enqueued = await get_memory_task_store().enqueue(
            "warm_extract",
            app_id=str(app_id),
            session_id=session_id,
            user_id=str(user_id or ""),
            payload={"agent_name": agent_name} if agent_name else None,
            dedup=True,
        )
        if enqueued is not None:
            log.debug("[funnel] 会话结束触发提炼: {}", session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("[funnel] 会话结束触发失败（非致命）: {}", exc)


# ── 内部 ─────────────────────────────────────────────────────────────────────

def _msg_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "; ".join(
            str(seg.get("content", "")) for seg in content if isinstance(seg, dict)
        )
    return str(content or "")


def _idle_expired(last_extract_at, minutes: int) -> bool:
    if not last_extract_at:
        return True  # 从未提炼过，有信号即视为超时
    try:
        from datetime import datetime
        if isinstance(last_extract_at, str):
            ref = datetime.fromisoformat(last_extract_at)
        else:
            ref = last_extract_at
        if ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        now = datetime.now()
        if ref > now:  # 时钟偏差防护
            return False
        return (now - ref).total_seconds() >= minutes * 60
    except (ValueError, TypeError):
        return True


def config_memory_trigger():
    from codegenx.ai_service.utils.config import config
    return config.memory.trigger


# ── Hook 监听器：记忆信号接入事件总线（docs/Hook设计.md §4.2） ───────────────


@on(HookEvent.TURN_END, name="memory_turn_signal", priority=20)
async def memory_turn_signal(ctx: HookContext) -> None:
    """本轮消息送入记忆提炼漏斗（P1-2，迁自 handlers._funnel_turn_signal）。

    depends_on persist_chat_snapshot：快照保存后再读末两条消息发信号。
    """
    session = ctx.session
    try:
        chat = (session.context.chat_messages if session.context else None) or []
        round_messages = [m for m in chat[-2:] if isinstance(m, dict)]
        if not round_messages:
            return
        await process_turn_signal(
            str(session.app_id),
            str(session.user_id or ""),
            session.session_id,
            round_messages,
            agent_name=str(getattr(session, "agent_name", "") or ""),
        )
    except Exception as exc:
        log.debug("记忆漏斗信号处理失败（非致命）: {}", exc)




