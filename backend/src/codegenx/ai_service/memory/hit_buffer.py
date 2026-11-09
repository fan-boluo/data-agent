"""
命中计数异步刷回（P1-7，设计方案 v2.1 §5.3）。

检索链路只做 Redis HINCRBY（mem:hit:{app_id}:{user_id} Hash，
field=memory_id 计数 + memory_id:ts 时间戳伴生字段，TTL 7d），
把热点行更新从检索路径挪到定时任务，避免高并发下拖慢每次查询。

定时任务（scheduler 每 5 分钟）SCAN mem:hit:* → HGETALL → 批量
UPDATE agent_memory → DEL 键。代价是 last_hit_at 最多 5 分钟延迟，
对衰减计算完全可接受。

Redis 不可用：当场回落 MySQL 直写（batch_touch_hit），不丢命中。
Redis 只做缓冲绝不做真源：缓冲丢失只是少计几次命中，无正确性影响。
"""
from __future__ import annotations

from shared import log
from codegenx.ai_service.memory import metrics

HIT_KEY_PREFIX = "mem:hit:"
HIT_TTL_SECONDS = 7 * 86400  # 7d


def _redis():
    from db.redis.redis_client import redis_client
    return redis_client


async def record_hits(app_id: str, user_id: str, memory_ids: list[str]) -> None:
    """检索命中登记（检索链路内调用，仅 Redis 写）。失败回落 MySQL 直写。"""
    if not memory_ids:
        return
    import time
    key = f"{HIT_KEY_PREFIX}{app_id}:{user_id}"
    try:
        pipe = _redis().pipeline(transaction=False)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        for mid in memory_ids:
            pipe.hincrby(key, mid, 1)
            pipe.hset(key, f"{mid}:ts", now_iso)
        pipe.expire(key, HIT_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 — Redis 故障降级为同步直写
        metrics.inc_degrade("redis")
        log.debug("hit 缓冲写失败，回落 DB 直写:{}", exc)
        await _fallback_direct(app_id, user_id, memory_ids)


async def _fallback_direct(app_id: str, user_id: str, memory_ids: list[str]) -> None:
    """Redis 不可用兜底：按 memory_id 查 id 后走原 DB 直写。"""
    try:
        from db.postgres.postgres_client import memory_fetch
        from codegenx.ai_service.memory.memory_store import batch_touch_hit
        rows = await memory_fetch(
            "SELECT id FROM memory.agent_memory "
            "WHERE app_id = $1 AND user_id = $2 AND memory_id = ANY($3::text[])",
            str(app_id), str(user_id), list(memory_ids),
        )
        await batch_touch_hit(app_id, user_id, [int(r[0]) for r in rows])
    except Exception as exc:  # noqa: BLE001 — 命中登记失败不影响检索
        log.debug("hit 兜底直写失败（非致命）:{}", exc)


async def flush_all() -> int:
    """刷回全部缓冲（scheduler 每 5 分钟调用）。返回刷回的记忆条数。

    SCAN（不 KEYS）逐键处理：HGETALL → 按租户分组 → apply_hit_flush → DEL。
    单键失败不影响其余键；UPDATE 成功才 DEL，崩溃重跑最多多刷一次（幂等无害）。
    """
    total_flushed = 0
    per_tenant: dict[tuple[str, str], dict[str, tuple[int, str]]] = {}
    try:
        cursor = 0
        keys: list[str] = []
        while True:
            cursor, batch = await _redis().scan(
                cursor=cursor, match=f"{HIT_KEY_PREFIX}*", count=200
            )
            keys.extend(batch)
            if cursor == 0:
                break
    except Exception as exc:  # noqa: BLE001 — Redis 不可用本轮放弃，缓冲留待下轮
        log.debug("[hit_flush] SCAN 失败（Redis 不可用？）:{}", exc)
        return 0

    for key in keys:
        try:
            # 前缀 mem:hit: 自含两个冒号，maxsplit=3；user_id 含冒号也安全
            _, _, app_id, user_id = key.split(":", 3)
            data = await _redis().hgetall(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("[hit_flush] 读取 {} 失败: {}", key, exc)
            continue
        if not data:
            continue
        hits: dict[str, tuple[int, str]] = {}
        for field, value in data.items():
            if field.endswith(":ts"):
                continue
            try:
                hits[field] = (int(value), str(data.get(f"{field}:ts", "")))
            except (TypeError, ValueError):
                continue
        if hits:
            per_tenant[(app_id, user_id)] = hits

    from codegenx.ai_service.memory.memory_store import apply_hit_flush
    for (app_id, user_id), hits in per_tenant.items():
        try:
            done = await apply_hit_flush(app_id, user_id, hits)
            await _redis().delete(f"{HIT_KEY_PREFIX}{app_id}:{user_id}")
            total_flushed += done
        except Exception as exc:  # noqa: BLE001 — 单租户失败留待下轮
            log.warning("[hit_flush] 租户 {}/{} 刷回失败（缓冲保留）: {}", app_id, user_id, exc)
    return total_flushed
