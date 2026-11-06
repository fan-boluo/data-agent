"""
Full compaction system — session-memory fast path + LLM summarization.

Two compaction paths (tried in order):
──────────────────────────────────────────
Path A — Session-memory fast path
  • Reads existing session MEMORY.md summary
  • Keeps last N messages that fit in MIN_TEXT_MESSAGES + MAX_TOKENS_AFTER
  • No LLM call — immediate, zero cost
  • Used when session memory has already been extracted at least once

Path B — LLM summarization
  • Sends the full conversation to the LLM with BASE_COMPACT_PROMPT
    （经韧性层 resilient_invoke：模型级熔断/同模型重试/链上降级都在其中）
  • Strips <analysis> block, uses <summary> block as context replacement

Failure handling（统一出口）
  • 任何「拿不到有效压缩结果」的场景——Path B 调用失败（韧性层已重试/降级/
    熔断跳过后仍失败）、返回空摘要、压缩无效（压缩后 token 不降反升）——
    都由 compact_if_needed 汇入同一个兜底：保守截断（保底可用，绝不向上抛错）。
    压缩无效属于业务结果而非模型故障，不进熔断。
  • Path A 不经 LLM、不受熔断影响（永远安全且零成本）；其读取失败仅降级
    尝试 Path B，不直接进入兜底。
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from codegenx.ai_service.llm.errors import classify_llm_error
from codegenx.ai_service.llm.resilience import get_breaker, resilient_invoke
from codegenx.ai_service.llm.async_client import get_llm
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.utils.config import config, split_model_chain
from codegenx.ai_service.compact.thresholds import (
    estimate_tokens,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
)
from codegenx.ai_service.compact.prompt import (
    BASE_COMPACT_PROMPT,
    format_compact_summary,
    get_compact_user_summary_message,
)

from shared import log

# ── Session-memory fast-path constants ────────────────────────────────────────
# Mirrors config in sessionMemoryCompact.ts (getSessionMemoryCompactConfig)

MIN_TEXT_MESSAGES = 5        # always keep at least this many user/assistant turns
MAX_TOKENS_AFTER  = 1_200    # token budget for kept messages (scale up for real LLM)
MIN_TOKENS_AFTER  = 200      # don't truncate below this even if over budget

# ── CompactResult type ────────────────────────────────────────────────────────

@dataclass
class CompactResult:
    """Outcome of a single compaction run."""
    messages:    list[dict]
    summary:     str
    path_used:   str   # "session_memory" | "llm" | "none"
    messages_removed: int = 0
    tokens_before:    int = 0
    tokens_after:     int = 0


@dataclass
class ContextBudgetResult:
    """一次 LLM 调用的上下文预算结果（token_budget 统一出口）。"""

    system_prompt: str
    tools: list[dict]
    payload_messages: list[dict]    # 预算后的消息视图（不含 system；组装由 context.assemble 负责）
    total_budget: int = 0
    tools_tokens_before: int = 0
    tools_tokens_after: int = 0
    system_tokens: int = 0
    message_budget: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    evicted_tools: int = 0
    evicted_histories: int = 0
    sections: list = field(default_factory=list)   # list[SectionReport]
    compact_path: str = ""
    truncated: bool = False
    micro_compacted: bool = False   # 工具结果微压缩实际生效

    @property
    def context_usage(self) -> float:
        """压缩后上下文占用率（payload 总 token / 总预算）。"""
        if not self.total_budget:
            return 0.0
        return round(
            (self.tools_tokens_after + self.system_tokens + self.tokens_after)
            / self.total_budget, 4,
        )

    @property
    def compacted(self) -> bool:
        """本次是否发生实际压缩（微压缩/摘要/驱逐/截断/分区驱逐/tools 瘦身）。"""
        return bool(
            self.micro_compacted
            or self.compact_path
            or self.evicted_tools
            or self.evicted_histories
            or self.truncated
            or self.tools_tokens_after < self.tools_tokens_before
            or any(getattr(s, "evicted", False) for s in self.sections)
        )

    def get_record(self) -> dict:
        """预算结果摘要（trace span attributes / 日志消费）。"""
        return {
            "total_budget": self.total_budget,
            "tools_tokens_before": self.tools_tokens_before,
            "tools_tokens_after": self.tools_tokens_after,
            "system_tokens": self.system_tokens,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "evicted_tools": self.evicted_tools,
            "evicted_histories": self.evicted_histories,
            "compact_path": self.compact_path or "",
            "truncated": self.truncated,
            "sections": {r.name: [r.before, r.after] for r in self.sections},
            "context_usage": self.context_usage,
        }


# ── Path A — session-memory fast path ─────────────────────────────────────────

def _has_text_content(msg: dict) -> bool:
    """True if this message has at least one plain-text content item."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(item, str) and item.strip()
            or (isinstance(item, dict) and item.get("type") == "text" and item.get("text", "").strip())
            for item in content
        )
    return False


def _keep_last_n_messages(
    messages: list[dict],
    min_keep: int = MIN_TEXT_MESSAGES,
    max_tokens: int = MAX_TOKENS_AFTER,
) -> list[dict]:
    """
    从尾部保留尽可能多的消息，在不超过 max_tokens 且至少 min_keep 条文本消息的前提下。
    保持与 _session_memory_compact 相同的截断逻辑。
    """
    kept: list[dict] = []
    text_count = 0
    token_acc = 0

    for msg in reversed(messages):
        msg_tokens = estimate_tokens([msg])
        if (
            token_acc + msg_tokens > max_tokens
            and text_count >= min_keep
            and token_acc > MIN_TOKENS_AFTER
        ):
            break
        kept.insert(0, msg)
        token_acc += msg_tokens
        if _has_text_content(msg):
            text_count += 1
    return kept


def _session_memory_compact(
    messages: list[dict], session_summary: str, *, watermark: int = 0,
    tail_budget: int | None = None,
) -> CompactResult:
    """
    Fast-path compaction using an already-extracted session summary.

    按水位拼接：前 watermark 条已由 SUMMARY.md 覆盖，直接以合成对替代；
    未摘要段（watermark 之后）在预算内从尾部保留，避免与摘要范围重叠/留缝。
    watermark=0（无水位）时退化为全量尾部保留。
    """
    tokens_before = estimate_tokens(messages)
    # Build the prior-context pair first so we can subtract its cost from the tail budget
    user_context = {
        "role": "user",
        "content": get_compact_user_summary_message(session_summary),
    }
    assistant_ack = {
        "role": "assistant",
        "content": (
            "I'll continue from where we left off. "
            "I have the context from the previous session."
        ),
    }
    synthetic_tokens = estimate_tokens([user_context, assistant_ack])
    # tail 预算优先用统一预算器传入的消息预算（封顶 MAX_TOKENS_AFTER），
    # 保证压缩结果 ≤ 调用方预算，避免「越压越大」被判无效
    target = MAX_TOKENS_AFTER if not tail_budget else max(
        MIN_TOKENS_AFTER, min(int(tail_budget), MAX_TOKENS_AFTER))
    tail_max = max(MIN_TOKENS_AFTER, target - synthetic_tokens)
    # 水位失效防御：超出列表长度按 0 处理（全量）
    if watermark >= len(messages):
        watermark = 0
    kept = _keep_last_n_messages(messages[watermark:], max_tokens=tail_max)
    # 最多保留 N-2 条消息，避免压缩后消息数反而增加
    max_tail = max(MIN_TEXT_MESSAGES, len(messages) - 2)
    if len(kept) > max_tail:
        kept = kept[-max_tail:]

    new_messages = [user_context, assistant_ack] + kept
    return CompactResult(
        messages=new_messages,
        summary=session_summary,
        path_used="session_memory",
        messages_removed=max(0, len(messages) - len(new_messages)),
        tokens_before=tokens_before,
        tokens_after=estimate_tokens(new_messages),
    )


# ── Path B — LLM summarization ────────────────────────────────────────────────

async def _call_llm_for_summary(messages: list[dict], chain: list[str]) -> str:
    """
    Send the full conversation plus the compact prompt to the compaction model
    chain（经韧性层 resilient_invoke：模型级熔断/同模型重试/链上降级都在其中），
    then return the cleaned summary string. 失败向上抛出，由调用方统一兜底。
    """
    compact_messages = list(messages) + [
        {"role": "user", "content": BASE_COMPACT_PROMPT}
    ]
    # 标准化内容，避免 dict/list 等非字符串 content 导致 LLM API 拒绝
    for msg in compact_messages:
        content = msg.get("content")
        if not isinstance(content, str):
            msg["content"] = str(content)

    # 摘要输出上限：不设限的摘要可能比原文还长（本测试曾出现 3178 token 摘要，
    # 压缩结果 +20% 被判无效），限幅后压缩结果才能进预算
    raw = await resilient_invoke(
        compact_messages, chain=chain, label="compact",
        max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    )
    return format_compact_summary(raw.get("content",""))


async def _llm_compact(
    messages: list[dict], chain: list[str], *, tail_budget: int | None = None,
) -> CompactResult | None:
    """
    Path B: ask the LLM to summarise the conversation, then rebuild messages.
    同模型重试/链上降级/熔断跳过已由韧性层（resilient_invoke）负责，此处不再
    自带重试循环；任何失败（含空摘要）只记日志并返回 None，由调用方
    compact_if_needed 统一走保守截断兜底。
    """
    tokens_before = estimate_tokens(messages)
    try:
        summary = await _call_llm_for_summary(messages, chain)
    except Exception as exc:
        log.warning(
            "LLM compact failed ({}): {}",
            classify_llm_error(exc).value, exc,
        )
        return None

    if not summary.strip():
        log.warning("LLM compact returned empty summary.")
        return None

    user_context = {
        "role": "user",
        "content": get_compact_user_summary_message(summary),
    }
    assistant_ack = {
        "role": "assistant",
        "content": (
            "Understood. I have the context from the previous session "
            "and will continue from where we left off."
        ),
    }

    # 保留最后 N 条非系统消息，避免摘要丢失最近的细节上下文
    # tail 预算优先用统一预算器传入的消息预算（封顶 MAX_TOKENS_AFTER），
    # 保证压缩结果 ≤ 调用方预算，避免「越压越大」被判无效
    synthetic_tokens = estimate_tokens([user_context, assistant_ack])
    target = MAX_TOKENS_AFTER if not tail_budget else max(
        MIN_TOKENS_AFTER, min(int(tail_budget), MAX_TOKENS_AFTER))
    tail_max = max(MIN_TOKENS_AFTER, target - synthetic_tokens)
    # 最多保留 N-2 条消息，避免压缩后消息数反而增加
    max_tail = max(MIN_TEXT_MESSAGES, len(messages) - 2)
    kept_tail = _keep_last_n_messages(messages, min_keep=MIN_TEXT_MESSAGES, max_tokens=tail_max)
    if len(kept_tail) > max_tail:
        kept_tail = kept_tail[-max_tail:]
    new_messages = [user_context, assistant_ack]
    if kept_tail:
        new_messages.extend(kept_tail)
    return CompactResult(
        messages=new_messages,
        summary=summary,
        path_used="llm",
        messages_removed=max(0, len(messages) - len(new_messages)),
        tokens_before=tokens_before,
        tokens_after=estimate_tokens(new_messages),
    )


# ── Unified auto-compact entry point ──────────────────────────────────────────


class CompactionService(BaseComponent):
    """压缩服务（服务化，原 CompactionEngine，docs/SystemApp架构设计.md §4.3）。

    - 模型：初始化时从 compact.model_name 解析（缺省回落默认模型），
      经韧性层 resilient_invoke 调用（模型级熔断/重试/降级都在其中）；
    - 失败兜底：调用失败/空摘要/压缩无效统一保守截断——压缩无效是业务结果
      而非模型故障，不进熔断（模型调用成败由韧性层模型级熔断器记录）；
    - 会话摘要（Path A 快速通道）经 summary_loader 读取（app.summary.load(ids)）。
    """

    name = ComponentType.COMPACTION

    def __init__(self, system_app=None) -> None:
        BaseComponent.__init__(self, system_app)
        # 初始化即解析压缩模型链（compact.model_name 逗号分隔，空则回落默认模型）
        self._model_chain: list[str] = split_model_chain(
            config.compact.model_name, config.get_default_model(),
        )
        # system 日志缓存（按 agent）：全量只打一次，之后分区内容变化才打印对应分区
        self._prompt_logged: set[str] = set()
        self._section_log_cache: dict[str, dict[str, str]] = {}

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    async def async_before_start(self) -> None:
        """预热压缩模型的客户端与熔断器（构造即注册，不发起网络请求；失败不阻断启动）。"""
        for model in self._model_chain:
            with suppress(Exception):
                get_llm(model)
                get_breaker(model)
                log.info("[compaction] 压缩模型客户端已预热: {}", model)

    @property
    def model_chain(self) -> list[str]:
        return list(self._model_chain)

# ------------------------预算分配------------------------------------------
    async def token_budget(
        self,
        *,
        profile: Any,
        prompt_params: dict[str, Any] | None,
        tools: list[dict] | None,
        context: Any,
        context_max_tokens: int = 0,
    ) -> ContextBudgetResult:
        """上下文预算的唯一编排入口（每次调 LLM 前执行一次）。

        总预算 B = agent.context_max_tokens × compact.context_budget_ratio。
        超限按驱逐级联处理，每级执行前判断超限，达标即停：
          1. 处理历史消息：micro_compact 工具结果压缩 → 仍超则 compact 消息摘要
             （context.compact：leader Path A 水位 / 子 agent Path B）
          2. 历史消息驱逐（enforce_budget_inplace：工具结果 → 中段 assistant 文本
             占位降级；user 提问承载任务上下文永不驱逐；保护最近 N 条工具结果）
          3. 保尾硬截断（仅截 payload 视图，不动真相源）
          4. system 分区驱逐（skill → memory → resource → expand → board → examples
             → tool_schema，逐个驱逐、逐个复量；空态分区（占位≥原文）跳过；
             skill/memory 可用工具捞回；tool_schema 只截描述不删工具——agent 靠
             调用工具完成任务，不到必要不降级）

        真相源策略（摘要闭环）：
          - 第 1/2 级就地修改 context.chat_messages（占位替换不改列表长度，
            摘要水位 extract_msg_idx 保持有效；compact 重建时水位重置 Path A=2 / Path B=0）；
          - 第 3 级只截 payload 视图、不动真相源——被截内容仍可被 turn 末摘要提取，闭环不破；
          - 历史占位消息对模型无信息量，payload 视图剔除（真相源保留以对齐水位）；
          - system 分区驱逐只影响当次 system_prompt（不写回 profile，下次超限会再次驱逐），
            tool_schema 只影响 tools 目录，摘要不覆盖二者，天然无冲突。

        职责边界：本方法只做各部分 token 管理（预算计算/驱逐级联/视图截断），
        不组装 payload——[system] + messages 由 context.assemble() 组装。
        """
        from codegenx.ai_service.compact.budget import (
            HISTORY_PLACEHOLDER,
            SECTION_EVICTION_ORDER,
            SectionReport,
            compress_tool_catalog,
            enforce_budget_inplace,
            schema_tokens,
            section_evicted_placeholder,
        )

        agent_name = str(getattr(context, "agent_name", "") or "agent")
        window = int(context_max_tokens or config.get_agent().context_max_tokens or 8000)
        total_budget = max(0, int(window * float(config.compact.context_budget_ratio)))

        # 标准化真相源（与旧 run() 每 step normalize 行为一致）
        await context.normalize_messages()
        msg_tokens_before = estimate_tokens(context.chat_messages)

        # system_prompt 预估 token
        resolved = profile.resolve_fields(prompt_params or {})
        system_prompt = profile.render(resolved)
        system_tokens = estimate_tokens([{"role": "system", "content": system_prompt}])

        # tools schema 预估（原样保留；瘦身排在 SECTION_EVICTION_ORDER 最后，不到必要不降级）
        tool_budget = max(0, int(total_budget * float(config.compact.tool_budget_ratio)))
        compressed_tools = list(tools or [])
        tools_before = tools_after = schema_tokens(compressed_tools)

        # 分区解析 + 首次渲染，记录各分区驱逐前 token（tool_schema 记原目录 tokens）
        section_before: dict[str, int] = {}
        for name in SECTION_EVICTION_ORDER:
            if name == "tool_schema":
                if tools_before > 0:
                    section_before[name] = tools_before
                continue
            value = resolved.get(name)
            if isinstance(value, str) and value:
                section_before[name] = estimate_tokens([{"content": value}])

        def _exceeded() -> bool:
            return tools_after + system_tokens + estimate_tokens(context.chat_messages) > total_budget

        def _msg_budget() -> int:
            return max(0, total_budget - tools_after - system_tokens)

        evicted_tools = 0
        evicted_histories = 0
        compact_path = ""
        truncated = False
        micro_compacted = False

        def _enforce(protect_last_n: int) -> None:
            nonlocal evicted_tools, evicted_histories
            rep = enforce_budget_inplace(
                context.chat_messages,
                budget=_msg_budget(),
                protect_last_n_tool_results=protect_last_n,
            )
            evicted_tools += rep.evicted_tools
            evicted_histories += rep.evicted_histories

        # ── 驱逐级联：先处理历史消息，仍超依次历史驱逐、分区驱逐，最后硬截断 ──
        if _exceeded():
            # 1. 处理历史消息：先压缩工具结果，仍超再做消息摘要（重建真相源并重置水位）
            _pre_micro = estimate_tokens(context.chat_messages)
            await context.micro_compact()
            micro_compacted = estimate_tokens(context.chat_messages) < _pre_micro
        if _exceeded() and _msg_budget() >= MIN_TOKENS_AFTER:
            async for data in context.compact(token_budget=_msg_budget()):
                compact_path = str((data or {}).get("path_used") or "compact")
                log.info("【预算】{} 触发上下文压缩 path={} {}→{}",
                            agent_name, compact_path,
                            (data or {}).get("tokens_before"), (data or {}).get("tokens_after"))

        # 视图绑定真相源：micro_compact/compact 会重建 chat_messages，组装前统一取最新列表
        payload_messages = context.chat_messages

        # 2. 仍超：历史消息驱逐（真相源占位降级；原文可经重新调用/快照回捞）
        if _exceeded():
            _enforce(int(config.compact.protect_last_n_results))

        # 3. 仍超：硬截断 payload 视图，不动真相源（保摘要闭环）
        if _exceeded():
            view = _keep_last_n_messages(
                context.chat_messages,
                min_keep=MIN_TEXT_MESSAGES,
                max_tokens=max(MIN_TOKENS_AFTER, _msg_budget()),
            )
            payload_messages = context.drop_orphan_tool_pairs(view)
            truncated = True
            log.warning("【预算】{} 降级为保尾硬截断（仅 payload 视图）：{} → {} tokens（budget={}）",
                        agent_name, estimate_tokens(context.chat_messages),
                        estimate_tokens(payload_messages), _msg_budget())

        # 4. 仍超：system 分区驱逐（逐个驱逐、逐个复量；skill/memory 可工具回捞）
        if _exceeded():
            for name in SECTION_EVICTION_ORDER:
                if not _exceeded():
                    break
                if section_before.get(name, 0) <= 0:
                    continue
                if name == "tool_schema":
                    # tools schema 瘦身（级联最后一环：截描述不删工具，释放计入 tools_after）
                    compressed_tools, _, tools_after = compress_tool_catalog(
                        compressed_tools,
                        budget=tool_budget,
                        description_max_chars=int(config.compact.tool_description_max_chars),
                    )
                    log.info("【预算】{} tool_schema 瘦身: {}→{} tokens（budget={}）",
                                agent_name, section_before[name], tools_after, tool_budget)
                    continue
                # 清空分区但原位留占位提示，让模型感知信息被临时移出、可尝试工具捞回；
                # 空态分区（如「暂无相关记忆」）占位比原文还大，驱逐无收益，跳过
                placeholder = section_evicted_placeholder(name)
                if estimate_tokens([{"content": placeholder}]) >= section_before[name]:
                    continue
                resolved[name] = placeholder
                system_prompt = profile.render(resolved)
                system_tokens = estimate_tokens([{"role": "system", "content": system_prompt}])
                log.info("【预算】{} 分区驱逐: {}（{} tokens → 占位提示）",
                            agent_name, name, section_before[name])


        # 历史占位消息对模型无信息量（原文无法经工具回捞），payload 视图直接剔除；
        # 真相源保留占位以维持摘要水位 extract_msg_idx 对齐
        payload_messages = [
            m for m in payload_messages if m.get("content") != HISTORY_PLACEHOLDER
        ]
        msg_tokens_after = estimate_tokens(payload_messages)

        # ── 日志：总账 + 逐分区结果（用户可见，每 step 一条）──────────────
        section_reports: list[SectionReport] = []
        for name in SECTION_EVICTION_ORDER:
            if name == "tool_schema":
                continue  # tools 瘦身在总账 tools=X→Y 单独体现，不计入 system 分区
            before = section_before.get(name, 0)
            final_val = resolved.get(name)
            after = estimate_tokens([{"content": final_val}]) if isinstance(final_val, str) and final_val else 0
            section_reports.append(SectionReport(
                name=name, before=before, after=after,
                evicted=before > 0 and final_val == section_evicted_placeholder(name),
            ))
        section_tokens = sum(r.after for r in section_reports)
        log.info(
            "【预算】{} 总预算={} tools={}→{} system={}(固定{}+分区{}) 消息={}→{}"
            " 驱逐[工具={} 历史={}] compact={} 截断={}",
            agent_name, total_budget, tools_before, tools_after, system_tokens,
            max(0, system_tokens - section_tokens), section_tokens,
            msg_tokens_before, msg_tokens_after,
            evicted_tools, evicted_histories,
            compact_path or "none", truncated,
        )
        log.info(
            "【预算】{} 分区: {}",
            agent_name,
            " | ".join(
                f"{r.name} {r.after}/{r.before}" + ("(驱逐)" if r.evicted else "")
                for r in section_reports
            ),
        )
        # system 日志（变化才打印）：首次打印全量；之后仅分区内容变化时打印该分区，
        # 固定区（含时间戳）不重复打印
        if agent_name not in self._prompt_logged:
            self._prompt_logged.add(agent_name)
            self._section_log_cache[agent_name] = {
                name: str(resolved.get(name) or "")
                for name in SECTION_EVICTION_ORDER if name != "tool_schema"
            }
            log.debug("【预算】{} system_prompt:\n{}", agent_name, system_prompt)
        else:
            cached = self._section_log_cache.setdefault(agent_name, {})
            for name in SECTION_EVICTION_ORDER:
                if name == "tool_schema":
                    continue
                val = resolved.get(name)
                val = val if isinstance(val, str) else ""
                if cached.get(name) != val:
                    cached[name] = val
                    log.debug("【预算】{} 分区[{}] 变化:\n{}", agent_name, name, val or "（空）")

        return ContextBudgetResult(
            system_prompt=system_prompt,
            tools=compressed_tools,
            payload_messages=payload_messages,
            total_budget=total_budget,
            tools_tokens_before=tools_before,
            tools_tokens_after=tools_after,
            system_tokens=system_tokens,
            message_budget=_msg_budget(),
            tokens_before=msg_tokens_before,
            tokens_after=msg_tokens_after,
            evicted_tools=evicted_tools,
            evicted_histories=evicted_histories,
            sections=section_reports,
            compact_path=compact_path,
            truncated=truncated,
            micro_compacted=micro_compacted,
        )

# ------------------------压缩------------------------------------------
    async def compact_if_needed(
        self,
        messages: list[dict],
        *,
        summary_loader: Any = None,          # Callable[[], str] | None
        summary_watermark: int = 0,          # 摘要覆盖到的内存 index 水位
        threshold: int | None = None,        # 统一预算器给出的消息预算
    ) -> tuple[list[dict], CompactResult | None]:
        """
        Run the full compaction pipeline if the threshold is exceeded.

        Returns (possibly_compacted_messages, result_or_None).
        result is None when compaction was skipped (not needed, or blocked).
        """
        from codegenx.ai_service.compact.thresholds import should_auto_compact

        if not should_auto_compact(messages, threshold=threshold):
            return messages, None

        tokens_before = estimate_tokens(messages)
        # 短对话直接跳过：摘要合成对 + 尾部保留的成本高于原文，压缩注定无效，
        # 交给预算级联的历史驱逐/硬截断处理，避免每 step 空耗一次摘要调用
        if tokens_before <= MAX_TOKENS_AFTER:
            log.info("【压缩】消息量较小（{}≤{} tokens），跳过摘要，交由预算级联驱逐/截断处理",
                     tokens_before, MAX_TOKENS_AFTER)
            return messages, None

        result = await self._run_compaction(
            messages, summary_loader=summary_loader, summary_watermark=summary_watermark,
            tail_budget=threshold,
        )
        if result is None or result.tokens_after >= result.tokens_before:
            # 统一失败出口：调用失败/空摘要（result=None）与压缩无效同等对待，
            # 保守截断兜底（压缩无效是业务结果，不属模型故障、不计熔断）
            if result is not None:
                log.info(
                    "Compaction ineffective ({}→{} tokens, +{:.0f}%); falling back to truncation.",
                    result.tokens_before, result.tokens_after,
                    (result.tokens_after - result.tokens_before) / max(1, result.tokens_before) * 100,
                )
            # Conservative truncation: keep last messages that fit within the
            # caller's message budget (falls back to the global effective window
            # for legacy callers without a threshold) so the result actually frees
            # context instead of returning the input unchanged.

            from codegenx.ai_service.compact.thresholds import EFFECTIVE_CONTEXT_WINDOW
            fallback_budget = (
                max(MIN_TOKENS_AFTER, int(threshold)) if threshold else EFFECTIVE_CONTEXT_WINDOW
            )
            truncated = _keep_last_n_messages(messages, min_keep=MIN_TEXT_MESSAGES, max_tokens=fallback_budget)
            log.warning(
                "All compaction paths failed; falling back to conservative truncation: {}→{} messages.",
                len(messages), len(truncated),
            )
            return truncated, CompactResult(
                messages=truncated,
                summary="",
                path_used="none",
                messages_removed=len(messages) - len(truncated),
                tokens_before=tokens_before,
                tokens_after=estimate_tokens(truncated),
            )

        log.info(
            "Compaction complete via {}: {}→{} tokens, removed {} messages.",
            result.path_used,
            result.tokens_before,
            result.tokens_after,
            result.messages_removed,
        )
        return result.messages, result

    # ----------------------------------------------------------------- private

    async def _run_compaction(
        self,
        messages: list[dict],
        *,
        summary_loader: Any = None,
        summary_watermark: int = 0,
        tail_budget: int | None = None,
    ) -> CompactResult | None:
        # Path A — session memory fast path（不经 LLM；读取失败仅降级尝试 Path B）
        if summary_loader is not None:
            try:
                summary = summary_loader()
                if summary and summary.strip():
                    log.info("Compaction: using session-memory fast-path.")
                    return _session_memory_compact(
                        messages, summary, watermark=summary_watermark, tail_budget=tail_budget)
            except Exception as exc:
                log.warning("Session-memory fast-path failed: {}; trying LLM.", exc)

        # Path B — LLM summarization（模型级熔断/重试/降级在韧性层内）
        result = await _llm_compact(messages, self._model_chain, tail_budget=tail_budget)
        if result is not None:
            return result

        log.warning("Compaction: all paths failed.")
        return None


def initialize_compaction(system_app) -> CompactionService:
    """注册压缩服务组件（system_app.initialize_components 调用）。"""
    return system_app.register(CompactionService)
