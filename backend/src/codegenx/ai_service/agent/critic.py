"""Critic —— Reflection 审查闭环（P0-6 最小版）。

定位：只读审查员（不挂任何执行类工具），对 lead 的最终答复做一次质量审查：
答非所问 / 数字无依据（与工具结果不符） / 口径或粒度错误 / 只给数不给结论。

接入点：LeaderAgent.observation（终答前）。verdict=fail 且重试次数未用尽时，
把审查建议作为「内部审查反馈」回填对话让 lead 修正后重新作答（最多 max_retry 次）；
重试用尽则标注「未通过审查」照常输出，禁止无限循环。

可观测：verdict 写 turn tags（critic_verdict / critic_ok），并落结构化日志。
默认关闭（config.critic.enabled=false），评测对比时再开。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from codegenx.ai_service.llm.resilience import resilient_invoke
from codegenx.ai_service.trace import add_turn_tags
from codegenx.ai_service.utils.config import config, split_model_chain
from shared import log

_CRITIC_SYSTEM = (
    "你是数据分析结果的质量审查员（Critic）。基于用户问题、工具执行证据（SQL 与查询结果）"
    "和待审查答复，判断答复是否可直接交付。审查维度：\n"
    "1. answered：是否正面回答了用户问题（不能答非所问或只复述过程）；\n"
    "2. grounded：答复中的关键数字/结论是否能在证据中找到依据（禁止编造数值）；\n"
    "3. issues：口径、粒度、时间范围是否与问题一致；是否只给数不给结论。\n"
    "只输出 JSON，不要输出其他文字：\n"
    '{"answered": true, "grounded": true, "issues": [], "suggestion": "", "score": 0.9}'
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class CriticVerdict:
    """结构化审查结论。"""

    answered: bool = True
    grounded: bool = True
    issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    score: float = 1.0

    @property
    def ok(self) -> bool:
        """通过审查：正面回答 + 数字有据 + 无问题项 + 分数达标。"""
        return self.answered and self.grounded and not self.issues and self.score >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "grounded": self.grounded,
            "issues": list(self.issues),
            "suggestion": self.suggestion,
            "score": self.score,
            "ok": self.ok,
        }


def _build_evidence(messages: list[dict[str, Any]], max_chars: int = 6000) -> str:
    """从对话中抽取工具证据（tool 结果为主，assistant 结论为辅），倒序取最近若干条。"""
    chunks: list[str] = []
    for m in reversed(messages or []):
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")
        if not content:
            continue
        if role == "tool":
            chunks.append(f"[工具结果] {content[:1200]}")
        elif role == "assistant":
            chunks.append(f"[assistant] {content[:800]}")
        else:
            continue
        if sum(len(c) for c in chunks) >= max_chars:
            break
    return "\n".join(reversed(chunks))[:max_chars] or "（无工具执行证据）"


def _parse_verdict(raw: str) -> CriticVerdict | None:
    """解析模型输出的 JSON（容忍 ```json 包裹与前后废话）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    issues = data.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    issues = [str(i).strip() for i in (issues or []) if str(i).strip()]
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return CriticVerdict(
        answered=bool(data.get("answered", False)),
        grounded=bool(data.get("grounded", False)),
        issues=issues,
        suggestion=str(data.get("suggestion") or "").strip(),
        score=max(0.0, min(1.0, score)),
    )


def should_review(answer: str, messages: list[dict[str, Any]]) -> bool:
    """是否需要送审（确定性门控，零额外模型调用）。

    只审「本轮有工具证据、且答复有实质内容」的结果：
    - 寒暄/闲聊/知识直答（本轮无工具调用）→ 不审；
    - 一句话短答复 → 不审。
    比意图路由更省：不引入额外的意图识别 LLM 调用。
    """
    text = str(answer or "").strip()
    if len(text) < int(config.critic.min_answer_chars or 0):
        return False
    # 从末尾往前找到本轮用户消息，检查其后的工具证据（历史消息里的旧工具结果不算）
    this_turn: list[dict[str, Any]] = []
    for m in reversed(messages or []):
        if m.get("role") == "user":
            break
        this_turn.append(m)
    return any(m.get("role") == "tool" for m in this_turn)


async def review_final_answer(
    *, question: str, answer: str, messages: list[dict[str, Any]],
) -> CriticVerdict | None:
    """审查最终答复；未启用或模型输出不可解析时返回 None（审查不可用不阻断主链路）。"""
    cfg = config.critic
    if not cfg.enabled or not should_review(answer, messages):
        return None

    chain = split_model_chain(cfg.model_name) or [config.get_default_model()]
    messages_payload = [
        {"role": "system", "content": _CRITIC_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【用户问题】\n{str(question or '')[:2000]}\n\n"
                f"【工具证据】\n{_build_evidence(messages)}\n\n"
                f"【待审查答复】\n{str(answer)[:4000]}"
            ),
        },
    ]
    try:
        response = await resilient_invoke(
            messages_payload, chain=chain, label="critic",
            max_tokens=600, temperature=0.0,
        )
        verdict = _parse_verdict(str((response or {}).get("content") or ""))
    except Exception as exc:
        log.warning("[critic] 审查调用失败（跳过审查）: {}", exc)
        return None
    if verdict is None:
        log.warning("[critic] 审查输出不可解析，跳过审查")
        return None

    # 可观测：turn tags 落 verdict（评测报告/排查可直接取）
    try:
        add_turn_tags(critic_verdict=json.dumps(verdict.to_dict(), ensure_ascii=False),
                      critic_ok=verdict.ok)
    except Exception:
        pass
    log.info("[critic] verdict ok={} score={:.2f} issues={} suggestion={}",
             verdict.ok, verdict.score, verdict.issues, verdict.suggestion[:120])
    return verdict
