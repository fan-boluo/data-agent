from __future__ import annotations

import re

from codegenx.ai_service.hook import HookContext, HookDecision, HookEvent, on

# 敏感词与注入模式：命中任一即 blocked
SENSITIVE_WORDS = [
    "忽略之前的指令",
    "ignore previous instructions",
    "ignore above",
    "破解",
    "hack",
    "绕过",
    "bypass",
    "越狱",
    "jailbreak",
]

INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:previous|above|all)\s+(?:instructions?|commands?|prompts?)"),
    re.compile(r"(?i)(?:forget|disregard)\s+(?:everything|all)\s+(?:above|before)"),
    re.compile(r"(?i)(?:pretend|act|behave)\s+(?:as|like)\s+(?:if|you\s+are)"),
    re.compile(r"(?i)system\s*:\s*you\s+are"),
    re.compile(r"(?i)new\s+(?:instructions?|commands?|prompts?)\s*:"),
]


@on(HookEvent.ON_USER_INPUT, name="inputput_safety_check", priority=10)
def inputput_safety_check(ctx: HookContext) -> HookDecision | None:
    """用户输入安全检查：空/超长/敏感词/注入模式命中即 blocked。"""
    user_input = ctx.data.get("user_input")
    text = (user_input or "").strip()
    # TODO 这两个校验放前端
    if not text:
        return HookDecision.block("输入内容不能为空")
    if len(text) > 1000:
        return HookDecision.block("输入内容过长，不要超过 1000 字")
    lowered = text.lower()
    for sensitive_word in SENSITIVE_WORDS:
        if sensitive_word.lower() in lowered:
            return HookDecision.block("输入包含不当内容，请修改后重试")

    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return HookDecision.block("检测到恶意输入，请求被拒绝")
    return None
