"""
向量同步与双数据源对账（P1-4/P1-5，设计方案 v2.1 §4.7/§7）。

分层职责：
  forward（P1-4）—— vector_synced_at IS NULL 即待办队列，调度循环每 30s
          批量补写（embedding + upsert 都必须批量）。天然幂等，崩溃自动续跑。
  reconcile（P1-5，每日 sync_check）—— 双向对账 + 抽检 + 进度回写：
    ① 正向：forward 再推一批（兜底当日残留）
    ② 反向：按租户 scroll Qdrant → 对照 MySQL 清理幽灵
    ③ 抽检：随机采样 active warm 用 summary 自检索，未命中自己 = embedding
       或索引有问题（召回质量问题不会报错，只会「助手变笨」）
    ④ 进度回写 memory_sync_checkpoint（断点续跑 + 审计，不承担同步正确性）

Qdrant collection 若被清空/损坏：数据无需手工恢复，vector_synced_at 全量置
NULL 后重跑即从 MySQL 完整重建。
"""
from __future__ import annotations

from sqlalchemy import text

from db.mysql.session import session_maker
from shared import log
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.llm.embedding import get_embedding_client
from codegenx.ai_service.memory.memory_store import (
    scan_pending_vector_sync,
    mark_vector_synced,
    count_pending_vector_sync,
    list_tenants,
    sample_active_warm,
)
from codegenx.ai_service.memory.vector_store import (
    upsert_points,
    search_by_vector,
    scroll_point_ids,
    delete_points_by_ids,
)
from codegenx.ai_service.schedule.memory_task_store import get_memory_task_store

# 正向单批上限与积压告警阈值（§10.1：> PENDING_ALERT_THRESHOLD 说明同步挂了）
_FORWARD_BATCH = 500
_PENDING_ALERT_THRESHOLD = 5000
_SPOT_CHECK_SAMPLES = 100
_SPOT_CHECK_MISS_ALERT = 10  # 抽检未命中超过该数告警


async def run_forward_sync_batch(batch_limit: int = _FORWARD_BATCH) -> int:
    """P1-4 向量同步 Worker 单轮：待同步队列 → 批量 embedding → 批量 upsert → 标记。

    调度循环每 30s 调一次（也供每日 reconcile 复用）。失败不标记
    （vector_synced_at 保持 NULL），下一轮自动重试；连续失败打点。
    """
    pending_total = await count_pending_vector_sync()
    metrics.set_vector_pending(pending_total)
    if pending_total > _PENDING_ALERT_THRESHOLD:
        log.error(
            "[vector_sync] 待同步队列积压 {} 条（>{}）,同步链路疑似故障",
            pending_total, _PENDING_ALERT_THRESHOLD,
        )
    if pending_total == 0:
        return 0

    entries = await scan_pending_vector_sync(limit=batch_limit)
    if not entries:
        return 0

    try:
        vectors = await get_embedding_client().embed_texts([e.inject_text() for e in entries])
    except Exception:
        metrics.inc_vector_sync_fail()
        metrics.inc_degrade("embedding")
        raise  # embedding 失败：不标记，下一轮重试
    try:
        await upsert_points(entries, vectors)
        await mark_vector_synced([e.id for e in entries])
    except Exception:
        metrics.inc_degrade("qdrant")
        metrics.inc_vector_sync_fail()
        raise
    log.debug("[vector_sync] 批次推送 {} 个点位（队列余量≈{}）", len(entries),
              max(0, pending_total - len(entries)))

    # 检查点仅作断点续跑记录（ULID 单调可比），不承担同步正确性
    try:
        await get_memory_task_store().advance_checkpoint(
            entries[0].app_id, "warm", entries[-1].memory_id, user_id=entries[0].user_id,
        )
    except Exception as exc:  # noqa: BLE001 — 检查点失败无害
        log.debug("[vector_sync] 检查点推进失败（非致命）:{}", exc)
    return len(entries)


async def reconcile_all_apps(llm_invoke=None) -> int:
    """每日对账一轮（llm_invoke 参数保留对齐 scheduler 签名，当前纯规则）。

    返回补写 + 清理的点位总数；抽检结果独立打点/告警。
    """
    missing = 0
    ghosted = 0
    try:
        missing = await run_forward_sync_batch()
        metrics.inc_reconcile(missing=missing)
    except Exception as exc:  # noqa: BLE001 — 单向失败不阻断另一向
        log.error("[sync_check] 正向补写异常: {}", exc)

    scanned = 0
    try:
        ghosted, scanned = await _reverse_sync()
        metrics.inc_reconcile(ghost=ghosted)
    except Exception as exc:  # noqa: BLE001
        log.error("[sync_check] 反向清理异常: {}", exc)

    try:
        spot_missed = await _spot_check()
        metrics.inc_reconcile(spot_miss=spot_missed)
    except Exception as exc:  # noqa: BLE001 — 抽检失败不影响对账结论
        log.warning("[sync_check] 抽检异常: {}", exc)
        spot_missed = -1

    # ④ 进度回写（全局行；断点取正向批次末尾或空串）
    try:
        await get_memory_task_store().record_reconcile(
            app_id="*", user_id="", scope="warm",
            scanned_rows=scanned, missing_found=missing, ghost_found=ghosted,
        )
    except Exception as exc:  # noqa: BLE001 — 回写失败无害
        log.debug("[sync_check] 进度回写失败（非致命）: {}", exc)

    log.info(
        "[sync_check] 完成: 补写={} 幽灵清理={} 扫描={} 抽检未命中={}",
        missing, ghosted, scanned, spot_missed,
    )
    return missing + ghosted


async def _reverse_sync() -> tuple[int, int]:
    """反向：按租户 scroll Qdrant → 对照 MySQL 清理幽灵点位。

    幽灵 = Qdrant 有但 MySQL 无（已物理删除/合规硬删）或 status != 1
    （已失效——正常情况会被正向重推覆盖，这里兜底清理漏推的）。
    返回 (清理数, 扫描点位总数)。
    """
    ghosts = 0
    scanned = 0
    for app_id, user_id in await list_tenants():
        point_ids = await scroll_point_ids(app_id, user_id)
        scanned += len(point_ids)
        if not point_ids:
            continue
        alive_status = await _status_by_ids(point_ids)
        dead_ids = [
            pid for pid in point_ids
            if alive_status.get(pid) is None or alive_status[pid] != 1
        ]
        if dead_ids:
            await delete_points_by_ids(dead_ids)
            ghosts += len(dead_ids)
            log.info("[sync_check] app {}/{} 清理幽灵点位 {} 个", app_id, user_id, len(dead_ids))
    return ghosts, scanned


async def _spot_check(limit: int = _SPOT_CHECK_SAMPLES) -> int:
    """③ 抽检：随机采样 active warm，用 summary 作为 query 自检索。

    命中自己 = 正常；未命中 = embedding 或索引有问题。返回未命中数。
    """
    samples = await sample_active_warm(limit=limit)
    if not samples:
        return 0
    missed = 0
    for entry in samples:
        try:
            query_vector = await get_embedding_client().embed_query(entry.inject_text())
            hits = await search_by_vector(
                app_id=entry.app_id,
                user_id=entry.user_id,
                query_vector=query_vector,
                limit=10,
                score_threshold=0.0,  # 抽检放宽阈值，看的是「能不能找回自己」
            )
        except Exception:  # noqa: BLE001 — 外部依赖抖动不算索引问题
            continue
        if entry.id not in {pid for pid, _score in hits}:
            missed += 1
    if missed > _SPOT_CHECK_MISS_ALERT:
        log.error(
            "[sync_check] 抽检未命中 {}/{}（>{}），embedding 或 Qdrant 索引疑似异常",
            missed, len(samples), _SPOT_CHECK_MISS_ALERT,
        )
    return missed


async def _status_by_ids(ids: list[int]) -> dict[int, int]:
    """按主键批量回查存活状态（id 来自 Qdrant 点位，主键精确回查无越权面）。"""
    async with session_maker() as session:
        rows = (
            await session.execute(
                text("SELECT id, status FROM agent_memory WHERE id IN :ids"),
                {"ids": tuple(int(i) for i in ids)},
            )
        ).all()
    return {int(r[0]): int(r[1]) for r in rows}
