"""
记忆写入器 —— 提炼候选 → 准入校验 → 分层落库（设计方案 v2.1 §4）。

写入语义（相对 v1 的关键简化，§4.4/§4.5）：
  hot  → uk_slot 确定性 upsert（同槽位旧记录自动 superseded），零 LLM 判重
  warm → append-only，不判重（重复反馈是晋升 hot 的依据，幂等由 memory_task 保证）
写入时同步的向量检索 + LLM 仲裁链路已整体移除——矛盾检测降级为每日异步
conflict_detect（P1）。

写入顺序：先 MySQL（真源）成立即写入成功；warm 向量层批量跟进，失败时
vector_synced_at 保持 NULL，由每日 sync_check 对账补写，不回滚。
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from shared import log
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.memory.models import (
    MemoryEntry,
    LAYER_HOT,
    SOURCE_INFERRED,
    BUILTIN_MEMORY_TYPES,
    layer_of,
    estimate_text_tokens,
)
from codegenx.ai_service.memory.memory_store import (
    append_warm,
    upsert_hot_slot,
    mark_vector_synced,
)
from codegenx.ai_service.llm.embedding import get_embedding_client
from codegenx.ai_service.memory.vector_store import upsert_points

# LLM 调用签名：v2 写入链路不再使用 LLM（判重已移除），参数保留以对齐
# scheduler 调用形态，conflict_detect（P1）复用同一约定。
LlmInvoke = Callable[..., Awaitable[str]]

# 敏感信息拦截（§4.3：prompt 与入库校验两处拦截，此为第二处）。
# 银行卡位段取 16-19：13 位段与毫秒时间戳撞车，误杀正常记忆，从宽处理。
_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\s*[:=]\s*\S+", "凭据赋值"),
    (r"sk-[A-Za-z0-9]{16,}", "API密钥"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥块"),
    (r"\b\d{17}[\dXx]\b", "身份证号"),
    (r"\b1[3-9]\d{9}\b", "手机号"),
    (r"\b\d{16,19}\b", "银行卡号"),
]


def match_sensitive(text: str) -> str | None:
    """命中返回敏感类别名，未命中返回 None。"""
    for pattern, label in _SENSITIVE_PATTERNS:
        if re.search(pattern, text or ""):
            return label
    return None


async def write_memories(
    user_id: str,
    app_id: str,
    session_id: str,
    candidates: list[dict],
    source_msg_ids: list[str] | None = None,
    task_id: int | None = None,
    source_agent: str | None = None,
) -> list[str]:
    """批量写入候选记忆，返回实际落库的 memory_id 列表（len 即条数）。

    单条失败跳过，不阻断整批。
    Args:
        source_msg_ids: 来源消息 message_uid 列表（P2-7 溯源，合规级联删除依赖）。
        task_id: 产生本批的 memory_task.id（P2-7 ③ 提炼批次反查）。
        source_agent: 产生记忆的智能体名（偏差①：写门控配套的来源元数据落库）。
    """
    if not candidates:
        return []
    store_cfg = config.memory.store
    hot_max = int(getattr(store_cfg, "hot_max_entries", 80) or 80)

    written_ids: list[str] = []
    warm_entries: list[MemoryEntry] = []
    for cand in candidates:
        try:
            entry = _build_entry(user_id, app_id, session_id, cand)
            if entry is None:
                continue
            if source_msg_ids:  # 溯源：消费区间内全部消息 uid（含 assistant）
                entry.source_msg_ids = [str(m) for m in source_msg_ids]
            if task_id:
                entry.task_id = int(task_id)
            if source_agent:  # 偏差①：来源智能体落库（来源审计/多智能体防污染定位）
                entry.source_agent = str(source_agent)
            if entry is None:
                continue
            if entry.memory_layer == LAYER_HOT:
                new_id = await upsert_hot_slot(
                    app_id, user_id, entry,
                    hot_max_entries=hot_max,
                    # 达硬上限转投 consolidate（P0-13），DAO 不反向依赖 scheduler
                    consolidate_enqueuer=_enqueue_consolidate,
                )
                if not new_id:
                    continue  # 硬上限拒绝（已 ERROR 日志 + 转投任务）
            else:
                new_id = await append_warm(app_id, user_id, entry)
                entry.id = new_id
                warm_entries.append(entry)
            written_ids.append(entry.memory_id)
        except Exception as exc:  # noqa: BLE001 — 单条失败不阻断整批
            log.error("记忆写入单条失败（跳过）: {} | {}", exc, str(cand)[:120])

    # warm 向量层批量跟进（批量 embedding + 批量 upsert，禁止逐条；失败留 sync_check）
    if warm_entries:
        try:
            vectors = await get_embedding_client().embed_texts(
                [e.inject_text() for e in warm_entries]
            )
        except Exception as exc:  # noqa: BLE001 — embedding 失败与向量库失败分开归因
            metrics.inc_degrade("embedding")
            log.warning("warm 向量 embedding 失败（vector_synced_at 留 NULL，待对账补写）:{}", exc)
            vectors = None
        if vectors is not None:
            try:
                await upsert_points(warm_entries, vectors)
                await mark_vector_synced([e.id for e in warm_entries])
            except Exception as exc:  # noqa: BLE001
                metrics.inc_degrade("qdrant")
                log.warning("warm 向量批量同步失败（vector_synced_at 留 NULL，待对账补写）:{}", exc)

    return written_ids


def _build_entry(user_id: str, app_id: str, session_id: str, cand: dict) -> MemoryEntry | None:
    """单条候选 → MemoryEntry；准入校验不过返回 None（拒绝原因打点，§10.1）。"""
    content = str(cand.get("content", "") or "").strip()
    if not content:
        metrics.inc_write_rejected("empty")
        return None
    memory_type = str(cand.get("memory_type", "") or "").strip()
    if memory_type not in BUILTIN_MEMORY_TYPES:
        metrics.inc_write_rejected("type")
        log.warning("未知记忆类型，拒绝写入（宁缺毋滥）: {}", memory_type)
        return None

    hit = match_sensitive(content)
    if hit:
        # §10.1 memory.write.rejected_sensitive
        metrics.inc_write_rejected("sensitive")
        log.warning("候选记忆含敏感信息({}),拦截: {}", hit, content[:40])
        return None

    source_type = int(cand.get("source_type") or SOURCE_INFERRED)
    confidence = float(cand.get("confidence") or 0.6)
    layer = layer_of(memory_type)

    # §4.3：模型推断不得进 hot 层（hot 只收用户明说/人工录入）
    if layer == LAYER_HOT and source_type == SOURCE_INFERRED:
        metrics.inc_write_rejected("inferred_hot")
        log.warning("模型推断的 hot 类候选被拒（推断不得进 hot 层）: {}", memory_type)
        return None

    return MemoryEntry(
        app_id=str(app_id),
        user_id=str(user_id),
        memory_type=memory_type,
        subject="self",  # P0 固定主体；多主体（项目/某人）随 P2 需求放开
        slot_key=str(cand.get("slot_key", "") or "").strip()[:64],
        summary=content[:500],
        content=content,
        topic=str(cand.get("topic", "") or "").strip()[:32],
        token_cost=estimate_text_tokens(content) + 4,
        source_type=source_type,
        confidence=round(min(max(confidence, 0.0), 1.0), 2),
        session_id=str(session_id or ""),
    )


async def _enqueue_consolidate() -> None:
    """hot 硬上限触发时转投整理任务（延迟导入避免循环依赖）。"""
    from codegenx.ai_service.schedule.memory_task_store import (
        get_memory_task_store,
        TASK_CONSOLIDATE,
    )
    await get_memory_task_store().enqueue(TASK_CONSOLIDATE, app_id="*")
