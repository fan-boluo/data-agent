"""
hot 层加载与注入（设计方案 v2.1 §5.1 / §5.4）。

真源为 MySQL agent_memory（memory_layer=1，memory_store.load_hot）；
hot 不进向量库、无时间衰减（P0-11），每轮对话全量注入 system prompt。

溢出策略（P0-13，相对 v1 的关键修正）：
  - 写入期：hot_max_entries 条数硬上限，超限拒绝写入并转投 consolidate
    （见 memory_store.upsert_hot_slot）
  - 运行期：万一仍超 token 预算，按裁剪优先级保留高优先级部分，但必须打
    ERROR 日志（告警指标为 P1-12）——静默截断 = 硬约束失效，是正确性事故
"""
from __future__ import annotations

from shared import log
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.memory.models import MemoryEntry, HOT_TYPE_PRIORITY, estimate_text_tokens


async def load_hot_entries(app_id: str, user_id: str) -> list[MemoryEntry]:
    """全部 active hot 记忆（MySQL 直读），按裁剪优先级（类型权重）+ updated_at 降序。

    排序在 Python 侧做：权重来自代码内常量表，避免 join 字典表 filesort；
    单用户 hot 条数有硬上限，排序成本可忽略（设计 §3.3）。
    """
    from codegenx.ai_service.memory.memory_store import load_hot
    entries = await load_hot(app_id, user_id)
    entries.sort(
        key=lambda e: (HOT_TYPE_PRIORITY.get(e.memory_type, 0.0), e.updated_at),
        reverse=True,
    )
    return entries


async def format_hot_prompt(app_id: str, user_id: str) -> str:
    """hot 层注入格式（每轮注入，保持极简）。超预算按优先级截断并 ERROR 告警。

    读取顺序（P1-1）：Redis 缓存 → miss 回落 MySQL 并回填；缓存里是排序后的
    最小注入视图（dict），预算裁剪每次现算（预算可在线调参）。
    """
    if not config.memory.search.enabled:
        return ""

    # ── 取条目：缓存优先 ─────────────────────────────────────────────────────────
    from codegenx.ai_service.memory import hot_cache
    cached = await hot_cache.get_cached_entries(app_id, user_id)
    if cached is not None:
        items = cached  # list[dict]
        hot_count = len(items)
    else:
        try:
            entries = await load_hot_entries(app_id, user_id)
        except Exception as exc:  # noqa: BLE001 — hot 加载失败不阻断对话（设计 §9 原则 1）
            metrics.inc_degrade("mysql")
            log.error("hot 层加载失败（本轮降级为无 hot 约束）:{}", exc)
            return ""
        hot_count = len(entries)
        await hot_cache.set_cached_entries(app_id, user_id, hot_cache.to_cache_items(entries))
        items = hot_cache.to_cache_items(entries)
    if not items:
        metrics.set_hot_ratio(0.0)
        return ""

    # ── token 预算裁剪（每次现算，配置可在线调整）────────────────────────────────
    budget = int(getattr(config.memory.search, "hot_token_budget", 2500) or 2500)
    kept: list[dict] = []
    used = 0
    dropped = 0
    for it in items:
        cost = int(it.get("token_cost") or 0) or (estimate_text_tokens(it.get("inject_text", "")) + 4)
        if used + cost > budget:
            dropped += 1
            continue
        kept.append(it)
        used += cost
    metrics.set_hot_ratio(used / max(1, budget))

    if dropped:
        # P0-13：截断必须可感知
        log.error(
            "hot 层超 token 预算({}/{}),截断 {} 条低优先级约束,需触发 consolidate 扩容/压缩",
            used, budget, dropped,
        )
    if not kept:
        return ""

    lines = ["# 核心约束（长期有效，优先级最高）"]
    for it in kept:
        lines.append(f"- {it.get('inject_text', '')}")
    return "\n".join(lines)
