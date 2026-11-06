"""
agent_memory_warm 向量检索层（Qdrant，设计方案 v2.1 §3.4 / §8）。

定位：MySQL agent_memory 是唯一真源，本层只是 warm 记忆的可重建检索索引——
清空 collection 后由 sync_check 从 MySQL 全量重放。本模块只做读写原语，
不做业务决策。

要点（P0-8）：
  - payload 瘦身：只存 memory_id / app_id / user_id / memory_type / status /
    created_at，正文不进 payload（回表取，§3.4.3）
  - point id = agent_memory.id（BIGINT；Qdrant 不接受 ULID）
  - 多租户：单 collection + payload 分区，user_id / app_id 建索引且
    is_tenant=true（§3.4.2）；检索必须带租户 filter
  - status 用 active/inactive 两档：recall 只取 active；失效记忆由
    vector_synced_at 置 NULL 触发全量重推（upsert 整体覆盖 payload）

qdrant-client 是同步 SDK，统一 asyncio.to_thread 包裹，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio

from qdrant_client import models

from db.qdrant.client import get_qdrant_client_manager
from shared import log
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.memory.models import MemoryEntry, STATUS_ACTIVE

WARM_COLLECTION = "agent_memory_warm"

# scroll 分页大小与页数上限（幽灵清理用；上限防异常数据拖死对账任务）
_SCROLL_PAGE_SIZE = 256
_SCROLL_MAX_PAGES = 200


def _payload_indexes() -> list[tuple[str, object]]:
    return [
        # is_tenant：Qdrant 针对字段优化索引布局，多租户过滤性能显著提升
        ("user_id", models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True)),
        ("app_id", models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True)),
        ("memory_type", models.PayloadSchemaType.KEYWORD),
        ("status", models.PayloadSchemaType.KEYWORD),
        ("created_at", models.PayloadSchemaType.DATETIME),
    ]


def point_payload(entry: MemoryEntry) -> dict:
    """瘦 payload：指针 + 过滤字段（§3.4.3），正文回 MySQL 取（get_active_by_ids）。"""
    return {
        "memory_id": entry.memory_id,
        "app_id": str(entry.app_id),
        "user_id": str(entry.user_id),
        "memory_type": entry.memory_type,
        "status": "active" if entry.status == STATUS_ACTIVE else "inactive",
        "created_at": entry.created_at,
    }


def _tenant_filter(app_id: str, user_id: str, active_only: bool = True) -> models.Filter:
    """租户过滤（§8：无 filter 的检索请求不允许存在）。"""
    must: list = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=str(user_id))),
        models.FieldCondition(key="app_id", match=models.MatchValue(value=str(app_id))),
    ]
    if active_only:
        must.append(models.FieldCondition(key="status", match=models.MatchValue(value="active")))
    return models.Filter(must=must)


async def ensure_warm_collection() -> None:
    """启动时确保 collection 与 payload 索引存在（幂等）。"""
    manager = get_qdrant_client_manager()
    await asyncio.to_thread(
        manager.ensure_collection,
        WARM_COLLECTION,
        config.embedding.dimensions,
        _payload_indexes(),
    )


async def upsert_points(entries: list[MemoryEntry], vectors: list[list[float]]) -> None:
    """写入/覆盖点位（id = agent_memory.id 整数）。状态变更靠整体重推。"""
    if not entries:
        return
    points = [
        models.PointStruct(
            id=int(entry.id),
            vector=vector,
            payload=point_payload(entry),
        )
        for entry, vector in zip(entries, vectors)
    ]
    client = get_qdrant_client_manager().client
    await asyncio.to_thread(
        client.upsert,
        collection_name=WARM_COLLECTION,
        points=points,
        wait=True,
    )


async def search_by_vector(
    app_id: str,
    user_id: str,
    query_vector: list[float],
    limit: int = 20,
    score_threshold: float | None = None,
) -> list[tuple[int, float]]:
    """语义检索，返回 (point_id=agent_memory.id, score) 列表，按分数降序。

    payload 不含正文，调用方（retriever）按 id 回 MySQL。
    """
    client = get_qdrant_client_manager().client
    print(f"[QDRANT] client → {client._url}")
    try:
        hits = await asyncio.to_thread(
            client.query_points,
            collection_name=WARM_COLLECTION,
            query=query_vector,
            query_filter=_tenant_filter(app_id, user_id, active_only=True),
            limit=limit,
            score_threshold=score_threshold or 0.0,
            with_payload=False,
        )
    except Exception as exc:  # noqa: BLE001 — 检索降级为空结果而非中断对话（§9）
        log.error("warm 向量检索失败:{}", exc)
        return []
    return [(int(p.id), float(p.score)) for p in hits.points]


async def delete_points_by_ids(ids: list[int]) -> None:
    """物理删除点位（归档同步 / 幽灵清理）。入参为 agent_memory.id。"""
    if not ids:
        return
    client = get_qdrant_client_manager().client
    selector = models.PointIdsList(points=[int(i) for i in ids])
    await asyncio.to_thread(
        client.delete,
        collection_name=WARM_COLLECTION,
        points_selector=selector,
        wait=True,
    )


async def delete_points_by_filter(
    app_id: str, user_id: str, memory_ids: list[str] | None = None
) -> None:
    """按 filter 物理删除（合规删除，§6.4 ③：一个请求搞定，无需先 scroll 出 id）。

    memory_ids=None 删该租户全部点位；指定时只删这些 memory_id。
    """
    client = get_qdrant_client_manager().client
    must: list = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=str(user_id))),
        models.FieldCondition(key="app_id", match=models.MatchValue(value=str(app_id))),
    ]
    if memory_ids:
        must.append(models.FieldCondition(
            key="memory_id", match=models.MatchAny(any=[str(m) for m in memory_ids]),
        ))
    try:
        await asyncio.to_thread(
            client.delete,
            collection_name=WARM_COLLECTION,
            points_selector=models.FilterSelector(filter=models.Filter(must=must)),
            wait=True,
        )
    except Exception as exc:  # noqa: BLE001 — 合规删除不因向量库故障中断（校验兜底）
        log.error("合规删除 Qdrant filter 删除失败（由 sync_check 幽灵清理兜底）:{}", exc)


async def scroll_point_ids(app_id: str, user_id: str) -> list[int]:
    """拉取租户全量点位 id（幽灵清理的反向对账输入）。"""
    client = get_qdrant_client_manager().client
    flt = _tenant_filter(app_id, user_id, active_only=False)
    ids: list[int] = []
    offset = None
    for _ in range(_SCROLL_MAX_PAGES):
        points, offset = await asyncio.to_thread(
            client.scroll,
            collection_name=WARM_COLLECTION,
            scroll_filter=flt,
            limit=_SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend(int(p.id) for p in points)
        if offset is None or not points:
            break
    if offset is not None:
        log.warning("warm 点位 scroll 达到分页上限({}),反向对账可能不完整", len(ids))
    return ids


async def count_points(app_id: str, user_id: str, active_only: bool = True) -> int:
    """对账/校验用计数。-1 表示查询失败（调用方决定降级语义）。"""
    client = get_qdrant_client_manager().client
    try:
        info = await asyncio.to_thread(
            client.count,
            collection_name=WARM_COLLECTION,
            count_filter=_tenant_filter(app_id, user_id, active_only=active_only),
            exact=True,
        )
        return info.count
    except Exception as exc:
        log.error("warm 点位计数失败:{}", exc)
        return -1
