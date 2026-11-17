"""输出护轨（P1-3，AFTER_LLM_INVOKE）：LLM 返回后的输出侧检查。

- PII 脱敏：手机号 / 邮箱 / 身份证号就地打码（改写 response.content）；
- 无依据数字检测：本轮无任何工具/子 agent 调用却输出统计数字 → 记录告警（不改写）；
- 越权表名拦截：答复含 SQL 且引用当前数据集之外的 schema → 追加警示（不改写结论）。

挂载点契约：AFTER_LLM_INVOKE 为并行分发、不可短路、异常吞掉；监听器只做
就地改写/追加与观测上报，不阻断主流程。import 即完成注册（base_react_agent 引入）。
"""
from __future__ import annotations

import re

from codegenx.ai_service.hook import HookContext, HookEvent, on
from codegenx.ai_service.utils.config import config
from shared import log

# 手机号（中国大陆 11 位）
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 身份证号（18 位，末位可 X）
_RE_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 邮箱
_RE_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
# 统计口径数字（千分位/小数/百分比），用于"无依据数字"检测
_RE_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)\.?\d*\s*%?(?![\w.])")
# 答复中的 schema.table 引用（配合 SQL 关键词出现才判定，降低误报）
_RE_SCHEMA_TABLE = re.compile(r"(?i)\b([a-z_][\w]*)\s*\.\s*([a-z_][\w]*)\b")
_RE_SQL_HINT = re.compile(r"(?i)\b(select|from|join)\b")
# 数据库内置 schema（不算越权）
_BUILTIN_SCHEMAS = {
    "information_schema", "pg_catalog", "performance_schema",
    "mysql", "sys", "public", "main", "sqlite", "temp",
}


def _mask_pii(text: str) -> str:
    """PII 打码：身份证留前 6 后 4，手机号留前 3 后 4，邮箱本地名留前 2 位。"""
    text = _RE_ID_CARD.sub(lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], text)
    text = _RE_PHONE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    text = _RE_EMAIL.sub(
        lambda m: m.group(0).split("@")[0][:2] + "***@" + m.group(0).split("@")[1], text
    )
    return text


def _cross_schema_warning(content: str, session) -> str:
    """答复含 SQL 且引用了当前数据源白名单之外的 schema 时，返回警示文案（无则空串）。"""
    if not _RE_SQL_HINT.search(content):
        return ""
    allowed = {s.split(".", 1)[0].lower() for s in (session.available_schemas or [])}
    current = str(getattr(session, "current_db", None) or "")
    if current and "." in current:
        allowed.add(current.split(".", 1)[0].lower())
    if not allowed:
        return ""
    bad = sorted({
        m.group(1).lower()
        for m in _RE_SCHEMA_TABLE.finditer(content)
        if m.group(1).lower() not in allowed
        and m.group(1).lower() not in _BUILTIN_SCHEMAS
    })
    if not bad:
        return ""
    return (
        f"⚠️ 注意：以上内容引用了当前数据集之外的表（{', '.join(bad)}），"
        "未经授权的数据不作为结论依据。"
    )


@on(HookEvent.AFTER_LLM_INVOKE, name="output_guardrail", priority=10)
async def output_guardrail(ctx: HookContext) -> None:
    """输出护轨监听器：就地改写/追加 + 告警上报，不短路不抛异常。"""
    if not config.guardrail.enabled:
        return
    response = ctx.data.get("response")
    if not isinstance(response, dict):
        return
    content = response.get("content")
    if not isinstance(content, str) or not content.strip():
        return
    session = ctx.session
    turn = getattr(session, "activate_turn", None)
    agent_name = str(ctx.data.get("agent_name") or "")

    # 1. PII 脱敏（就地改写）
    if config.guardrail.pii_mask_enabled:
        masked = _mask_pii(content)
        if masked != content:
            log.info("[output_guardrail] PII 脱敏生效 agent={}", agent_name)
            content = masked

    # 2. 无依据数字检测：本轮无工具/子 agent 证据却输出统计数字 → 告警（不改写）
    if config.guardrail.ungrounded_number_check and turn is not None:
        has_evidence = bool(turn.tool_call_counts) or bool(turn.active_sub_agents)
        numbers = _RE_NUMBER.findall(content)
        if numbers and not has_evidence:
            log.warning("[output_guardrail] 无工具依据的数字输出 agent={} numbers={}",
                        agent_name, numbers[:10])
            ctx.data["groundedness_warning"] = True

    # 3. 越权表名拦截：追加警示（不改写结论本体）
    if config.guardrail.cross_schema_check and session is not None:
        warning = _cross_schema_warning(content, session)
        if warning:
            log.warning("[output_guardrail] 越权表名警示 agent={}", agent_name)
            content = f"{content}\n\n{warning}"

    response["content"] = content
