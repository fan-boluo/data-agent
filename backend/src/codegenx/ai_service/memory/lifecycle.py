"""
记忆生命周期 —— 每日整理（consolidate）与衰减归档（decay_archive），设计方案 v2.1 §6。

P0-11 关键修正：hot 层不做时间衰减——hot 是全量加载不走召回，hit_count 不随
时间增长，按未访问衰减会把硬约束静默删掉（正确性事故）。衰减只作用于 warm。

流程（每日由 MemoryScheduler 定时触发，纯规则不耗 LLM）：
  consolidate    warm 精确重复合并（hot 压缩为 P2-2）
  decay_archive  warm 30 天未命中软删（status=4，可恢复）
                 → 软删后再 60 天（累计 90）导出归档 + valid_to 置位（不再参与召回），
                   PostgreSQL 保留 status=4 行（valid_to 置位防重复导出）

归档介质：OSS 为后续项，P0 导出本地 zip（.data/{user}/{app}/memory/archive/，
按月 jsonl 追加，与 v1 归档格式兼容）。
"""
from __future__ import annotations

import json
import zipfile

from shared import log
from shared.constants import get_memory_dir
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.memory.models import MemoryEntry
from codegenx.ai_service.memory.memory_store import (
    list_tenants,
    merge_duplicate_warm,
    decay_warm_soft_delete,
    scan_archivable,
    mark_archived_exported,
)


def _decay_days() -> int:
    return int(getattr(config.memory.store, "decay_days", 30) or 30)


def _archive_days() -> int:
    return int(getattr(config.memory.store, "archive_days", 90) or 90)


async def consolidate_all_apps() -> int:
    """全租户整理：warm 精确重复合并。单租户失败不阻断其他。返回合并条数。"""
    try:
        tenants = await list_tenants()
    except Exception as exc:  # noqa: BLE001 — MySQL 不可用时本轮放弃，下日重试
        log.error("[consolidate] 租户枚举失败:{}", exc)
        return 0
    total_merged = 0
    for app_id, user_id in tenants:
        try:
            merged = await merge_duplicate_warm(app_id, user_id)
            total_merged += int(merged or 0)
            if merged:
                log.info("[consolidate] app {}/{} 合并精确重复 warm {} 条", app_id, user_id, merged)
        except Exception as exc:  # noqa: BLE001
            log.error("[consolidate] app {}/{} 整理异常: {}", app_id, user_id, exc)
    return total_merged


async def decay_and_archive_all_apps() -> tuple[int, int]:
    """全局衰减软删（单条批量 SQL，无需按租户循环）→ 按租户归档导出。

    返回 (软删条数, 归档导出条数)，供 scheduler 打治理指标。
    """
    soft_deleted = 0
    # ── 软删：只作用 warm（P0-11），active_slot/vector_synced_at 由 DAO 同步维护 ──
    try:
        soft_deleted = await decay_warm_soft_delete(_decay_days())
        if soft_deleted:
            log.info("[decay_archive] warm 软删 {} 条（{} 天未命中，可恢复）", soft_deleted, _decay_days())
    except Exception as exc:  # noqa: BLE001
        log.error("[decay_archive] warm 软删异常: {}", exc)

    # ── 归档：status=4 且软删后再满 (archive_days - decay_days) 天 ──────────────
    gap_days = max(1, _archive_days() - _decay_days())
    try:
        rows = await scan_archivable(gap_days)
    except Exception as exc:  # noqa: BLE001
        log.error("[decay_archive] 归档扫描异常: {}", exc)
        return soft_deleted, 0
    if not rows:
        return soft_deleted, 0

    by_tenant: dict[tuple[str, str], list[MemoryEntry]] = {}
    for row in rows:
        by_tenant.setdefault((row.app_id, row.user_id), []).append(row)

    exported_ids: list[int] = []
    for (app_id, user_id), items in by_tenant.items():
        try:
            _export_archive_zip(app_id, user_id, items)
            exported_ids.extend(e.id for e in items)
            log.info("[decay_archive] app {}/{} 归档导出 {} 条", app_id, user_id, len(items))
        except Exception as exc:  # noqa: BLE001 — 单租户导出失败不影响其他
            log.error("[decay_archive] app {}/{} 归档导出异常: {}", app_id, user_id, exc)

    if exported_ids:
        try:
            await mark_archived_exported(exported_ids)  # valid_to 置位，防重复导出
        except Exception as exc:  # noqa: BLE001
            log.error("[decay_archive] 归档位标记失败（下轮将重复导出）: {}", exc)
        # 向量与记忆同表（pgvector）：valid_to 置位后行已不参与召回，无需外部清理

    return soft_deleted, len(exported_ids)


def _export_archive_zip(app_id: str, user_id: str, items: list[MemoryEntry]) -> None:
    """按月分组追加写入本地 zip（无 OSS 时的归档介质，见模块 docstring）。"""
    archive_dir = get_memory_dir(user_id, app_id) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for e in items:
        month = (e.created_at or "")[:7].replace("-", "") or "unknown"
        by_month.setdefault(month, []).append(e.to_row_dict())
    for month, payload in by_month.items():
        zip_path = archive_dir / f"agent_memory_{month}.zip"
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"agent_memory_{month}.jsonl",
                        "\n".join(json.dumps(r, ensure_ascii=False) for r in payload) + "\n")
