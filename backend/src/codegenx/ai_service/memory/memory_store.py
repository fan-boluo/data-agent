"""
agent_memory 表 DAO —— 记忆唯一真源（PostgreSQL + pgvector）。

隔离硬约束（设计 §8）：所有查询强制携带 app_id + user_id，本模块是唯一入口，
禁止业务代码绕过直接拼 SQL。
失效硬约束（设计 §3.3）：任何 status 1→2/3/4 的流转必须走 mark_inactive()——
同一语句内置 active_slot=NULL，否则该 uk_memory_slot 槽位永久无法写入新记忆。

与 MySQL+Qdrant 版本的差异（同库单表）：
  - 向量与记忆行同表同事务写入（embedding 列），不再有「行级同步队列」，
    也不再有 Qdrant 漏点/幽灵点的对账补偿；
  - 语义召回直接在这里用 pgvector 的 `<=>` 余弦距离完成，无需回表；
  - 关键词兜底通道走 pg_trgm（summary GIN 索引），替代 LIKE 全表扫。
"""
from __future__ import annotations

import json

from db.postgres.postgres_client import (
    memory_execute,
    memory_fetch,
    memory_fetchrow,
    memory_fetchval,
    memory_transaction,
)
from shared import log
from codegenx.ai_service.memory.models import MemoryEntry, LAYER_WARM, LAYER_HOT

# 行选取列（from_row 的输入契约，所有 SELECT 复用；不含 embedding，避免大字段回传）
_ROW_COLS = (
    "id, memory_id, app_id, user_id, memory_layer, memory_type, subject, slot_key, "
    "summary, content, token_cost, source_type, confidence, status, superseded_by, "
    "session_id, source_msg_ids, task_id, source_agent, hit_count, last_hit_at, "
    "created_at, updated_at"
)


def _rows_to_entries(rows) -> list[MemoryEntry]:
    return [MemoryEntry.from_row(dict(r)) for r in rows]


def vector_text(vec: list[float] | None) -> str:
    """向量 → pgvector 文本格式；None/空返回空串（调用方走关键词模式）。

    以文本参数传入再 `::vector` 转换，避免 asyncpg 缺少 vector 类型编解码器
    （与 semantic/store/pg_store.py 同一约定）。
    """
    if not vec:
        return ""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


# ── hot 层读取（P0-5：读文件 → 查 SQL）────────────────────────────────────────

async def load_hot(app_id: str, user_id: str) -> list[MemoryEntry]:
    """全部 active hot 记忆（每轮对话开始全量加载注入 system prompt）。"""
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS} FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2 AND memory_layer = 1 AND status = 1 "
        "AND (valid_to IS NULL OR valid_to > now())",
        str(app_id), str(user_id),
    )
    return _rows_to_entries(rows)


async def count_hot_slots(app_id: str, user_id: str) -> int:
    """当前 active hot 条数（写入期硬上限检查，设计 §5.4）。"""
    val = await memory_fetchval(
        "SELECT COUNT(*) FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2 AND memory_layer = 1 AND status = 1",
        str(app_id), str(user_id),
    )
    return int(val or 0)


# ── hot 层写入（P0-7：uk_slot 确定性 upsert，不同步调 LLM 判重）────────────────

async def upsert_hot_slot(
    app_id: str,
    user_id: str,
    entry: MemoryEntry,
    *,
    hot_max_entries: int = 80,
    consolidate_enqueuer=None,
) -> int:
    """hot 确定性写入：同槽位旧 active → superseded（释放槽位），插入新 active。

    Args:
        hot_max_entries: 写入期条数硬上限，达到时拒绝写入并回调
            consolidate_enqueuer（转投整理任务），不静默截断。
        consolidate_enqueuer: async callable() -> None，由调用方注入
            （避免 DAO 反向依赖 scheduler）。

    Returns:
        新行 id；达到硬上限被拒绝时返回 0。
    """
    count = await count_hot_slots(app_id, user_id)
    if count >= hot_max_entries:
        # 运行期不静默截断（P0-13）：写入拒绝 + 转投 consolidate，等压缩后再写
        log.error(
            "hot 层达到条数硬上限({}/{}),拒绝写入并转投 consolidate: {}",
            count, hot_max_entries, entry.summary[:60],
        )
        if consolidate_enqueuer is not None:
            try:
                await consolidate_enqueuer()
            except Exception as exc:  # noqa: BLE001 — 投递失败不影响拒绝语义
                log.warning("consolidate 任务投递失败:{}", exc)
        return 0

    entry.memory_layer = LAYER_HOT
    entry.slot_key = entry.slot_key or entry.memory_id
    p = _insert_params(entry)
    # 两条语句同事务：先释放同槽位旧 active，再插入新 active（hot 不进向量，embedding 恒 NULL）
    async with memory_transaction() as conn:
        await conn.execute(
            "UPDATE agent.agent_memory "
            "SET status = 2, active_slot = NULL, superseded_by = $1 "
            "WHERE app_id = $2 AND user_id = $3 AND subject = $4 "
            "AND memory_type = $5 AND slot_key = $6 AND active_slot = 1",
            entry.memory_id, str(app_id), str(user_id),
            entry.subject, entry.memory_type, entry.slot_key,
        )
        new_id = await conn.fetchval(
            "INSERT INTO agent.agent_memory "
            "(app_id, user_id, memory_id, memory_layer, memory_type, subject, slot_key, "
            " active_slot, summary, content, token_cost, source_type, confidence, status, "
            " session_id, source_msg_ids, task_id, source_agent) "
            "VALUES ($1, $2, $3, 1, $4, $5, $6, 1, $7, $8::jsonb, $9, $10, $11, 1, "
            " $12, $13::jsonb, $14, $15) "
            "RETURNING id",
            p["a"], p["u"], p["mid"], p["t"], p["subj"], p["slot"], p["sum"],
            p["content"], p["tc"], p["src"], p["conf"],
            p["sid"], p["msgs"], p["task"], p["sagent"],
        )
    # P1-1 写时 DEL：hot 变更后下次读触发缓存重建
    from codegenx.ai_service.memory import hot_cache
    await hot_cache.invalidate(str(app_id), str(user_id))
    return int(new_id or 0)


# ── warm 层写入（P0-7：append-only，不判重）────────────────────────────────────

async def append_warm_embedded(app_id: str, user_id: str, entry: MemoryEntry) -> int:
    """管理端手工写入 warm：先算向量再落库，保证新记忆立即可被语义召回。

    embedding 失败向上抛（管理端需要明确失败，而不是写入一条检索不到的记录）。
    """
    from codegenx.ai_service.llm.embedding import get_embedding_client
    vec = (await get_embedding_client().embed_texts([entry.inject_text()]))[0]
    return await append_warm(app_id, user_id, entry, embedding=vec)


async def append_warm(
    app_id: str, user_id: str, entry: MemoryEntry, embedding: list[float] | None = None
) -> int:
    """warm 追加写入：slot_key=memory_id，向量与行同语句落库。

    情景记忆不判重（设计 §4.5）：重复反馈是晋升 hot 的依据，幂等由
    memory_task 保证。调用方必须先拿到 embedding（pgvector 无异步补写通道，
    避免产生「行存在但检索不到」的不可见记忆）。返回新行 id。
    """
    entry.memory_layer = LAYER_WARM
    entry.slot_key = entry.memory_id  # warm 槽位即自身，uk_slot 天然不冲突
    p = _insert_params(entry)
    new_id = await memory_fetchval(
        "INSERT INTO agent.agent_memory "
        "(app_id, user_id, memory_id, memory_layer, memory_type, subject, slot_key, "
        " active_slot, summary, content, token_cost, source_type, confidence, status, "
        " session_id, source_msg_ids, task_id, source_agent, embedding) "
        "VALUES ($1, $2, $3, 2, $4, $5, $6, 1, $7, $8::jsonb, $9, $10, $11, 1, "
        " $12, $13::jsonb, $14, $15, $16::text::vector) "
        "RETURNING id",
        p["a"], p["u"], p["mid"], p["t"], p["subj"], p["slot"], p["sum"],
        p["content"], p["tc"], p["src"], p["conf"],
        p["sid"], p["msgs"], p["task"], p["sagent"],
        vector_text(embedding) or None,
    )
    return int(new_id or 0)


def _insert_params(entry: MemoryEntry) -> dict:
    return {
        "a": str(entry.app_id), "u": str(entry.user_id),
        "mid": entry.memory_id, "t": entry.memory_type, "subj": entry.subject,
        "slot": entry.slot_key, "sum": entry.inject_text()[:512],
        "content": entry.content_json(), "tc": int(entry.token_cost),
        "src": int(entry.source_type), "conf": float(entry.confidence),
        "sid": entry.session_id,
        "msgs": (
            json.dumps(entry.source_msg_ids, ensure_ascii=False)
            if entry.source_msg_ids else None
        ),
        "task": entry.task_id,
        "sagent": str(entry.source_agent or "") or None,
    }


# ── 失效（单一入口，设计 §3.3 硬约束）──────────────────────────────────────────

async def mark_inactive(
    app_id: str,
    user_id: str,
    memory_ids: list[str],
    status: int,
    superseded_by: str = "",
) -> int:
    """批量失效（superseded=2 / revoked=3 / archived=4）。

    置 active_slot=NULL 释放槽位；向量与行同表，状态变更即对召回生效，
    无需再向外部向量库重推 payload。
    """
    if not memory_ids:
        return 0
    tag = await memory_execute(
        "UPDATE agent.agent_memory "
        "SET status = $1, active_slot = NULL, superseded_by = $2 "
        "WHERE app_id = $3 AND user_id = $4 AND memory_id = ANY($5::text[]) AND status = 1",
        int(status), superseded_by, str(app_id), str(user_id), list(memory_ids),
    )
    # 可能涉及 hot 行（conflict_detect/人工撤销走这里），写时 DEL 缓存
    from codegenx.ai_service.memory import hot_cache
    await hot_cache.invalidate(str(app_id), str(user_id))
    return _affected(tag)


# ── 检索（pgvector 语义 + pg_trgm 关键词，均在同表完成）────────────────────────

async def search_by_vector(
    app_id: str,
    user_id: str,
    query_vector: list[float],
    limit: int = 20,
    score_threshold: float | None = None,
) -> list[tuple[MemoryEntry, float]]:
    """语义召回：返回 (记忆, 余弦相似度) 列表，按相似度降序。

    `<=>` 是余弦距离（0~2），相似度 = 1 - 距离。只扫 partial HNSW 索引
    覆盖的「warm + active」行，租户过滤强制携带。
    """
    qv = vector_text(query_vector)
    if not qv:
        return []
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS}, 1 - (embedding <=> $1::text::vector) AS score "
        "FROM agent.agent_memory "
        "WHERE app_id = $2 AND user_id = $3 AND memory_layer = 2 AND status = 1 "
        "AND embedding IS NOT NULL "
        "ORDER BY embedding <=> $1::text::vector LIMIT $4",
        qv, str(app_id), str(user_id), int(limit),
    )
    out: list[tuple[MemoryEntry, float]] = []
    for r in rows:
        d = dict(r)
        score = float(d.pop("score") or 0.0)
        if score_threshold is not None and score < float(score_threshold):
            continue
        out.append((MemoryEntry.from_row(d), score))
    return out


async def search_summary_keyword(
    app_id: str, user_id: str, keyword: str, limit: int = 10
) -> list[tuple[MemoryEntry, float]]:
    """关键词兜底通道：pg_trgm 相似度匹配 summary（标识符/表名/路径等 embedding 弱项）。

    `%` 走 GIN 三元组索引；再按 similarity 降序，避免全表排序。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS}, similarity(summary, $1) AS score "
        "FROM agent.agent_memory "
        "WHERE app_id = $2 AND user_id = $3 AND memory_layer = 2 AND status = 1 "
        "AND summary % $1 "
        "ORDER BY score DESC LIMIT $4",
        kw, str(app_id), str(user_id), int(limit),
    )
    return [(MemoryEntry.from_row(dict(r)), float(dict(r).get("score") or 0.0)) for r in rows]


async def get_active_by_ids(app_id: str, user_id: str, ids: list[int]) -> list[MemoryEntry]:
    """按自增 id 回表；只回 active（管理/排查工具用）。"""
    if not ids:
        return []
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS} FROM agent.agent_memory "
        "WHERE id = ANY($1::bigint[]) AND app_id = $2 AND user_id = $3 "
        "AND memory_layer = 2 AND status = 1",
        [int(i) for i in ids], str(app_id), str(user_id),
    )
    return _rows_to_entries(rows)


async def batch_touch_hit(app_id: str, user_id: str, ids: list[int]) -> None:
    """召回命中登记：hit_count+1、last_hit_at=now()（衰减与软删依据）。

    P1-7 后仅作 Redis 不可用时的兜底路径；常规是 Redis Hash 聚合 + 定时批量刷回。
    """
    if not ids:
        return
    try:
        await memory_execute(
            "UPDATE agent.agent_memory SET hit_count = hit_count + 1, last_hit_at = now() "
            "WHERE id = ANY($1::bigint[]) AND app_id = $2 AND user_id = $3",
            [int(i) for i in ids], str(app_id), str(user_id),
        )
    except Exception as exc:  # noqa: BLE001 — 命中登记失败不影响检索
        log.debug("batch_touch_hit 失败（非致命）:{}", exc)


async def apply_hit_flush(app_id: str, user_id: str, hits: dict[str, tuple[int, str]]) -> int:
    """P1-7 刷回落地：按 memory_id 批量回写聚合命中（count, last_hit ISO）。

    单条 UPDATE ... FROM (VALUES ...) 一次提交，避免逐行往返。
    返回成功行数；调用方保证仅在整批成功后 DEL 缓存键。
    """
    if not hits:
        return 0
    mids: list[str] = []
    counts: list[int] = []
    stamps: list = []
    for memory_id, (count, hit_at) in hits.items():
        mids.append(memory_id)
        counts.append(int(count))
        stamps.append(_iso_to_dt(hit_at))
    try:
        tag = await memory_execute(
            "UPDATE agent.agent_memory AS m "
            "SET hit_count = m.hit_count + v.c, "
            "    last_hit_at = COALESCE(v.ts, m.last_hit_at) "
            "FROM (SELECT * FROM unnest($1::text[], $2::int[], $3::timestamptz[]) "
            "      AS t(memory_id, c, ts)) AS v "
            "WHERE m.app_id = $4 AND m.user_id = $5 AND m.memory_id = v.memory_id",
            mids, counts, stamps, str(app_id), str(user_id),
        )
        return _affected(tag)
    except Exception as exc:  # noqa: BLE001 — 刷回失败非致命，下轮重试
        log.warning("[hit_flush] 批量刷回失败（将重试）: {}", exc)
        return 0


def _iso_to_dt(ts: str):
    """ISO 字符串 → datetime；坏值返回 None（保持原 last_hit_at 不动）。"""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _affected(tag: str) -> int:
    """asyncpg 命令标签 'UPDATE 3' → 影响行数。"""
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


# ── consolidate（每日整理：warm 精确重复合并）─────────────────────────────────

async def merge_duplicate_warm(app_id: str, user_id: str) -> int:
    """同 summary 精确重复的 warm 记忆只保留最新（id 最大），旧的置 superseded。

    PG 无 UPDATE ... JOIN 语法，改用 UPDATE ... FROM 子查询等价的写法。
    """
    tag = await memory_execute(
        "UPDATE agent.agent_memory AS m "
        "SET status = 2, active_slot = NULL "
        "FROM ("
        "  SELECT summary, MAX(id) AS keep_id FROM agent.agent_memory "
        "  WHERE app_id = $1 AND user_id = $2 AND memory_layer = 2 AND status = 1 "
        "  GROUP BY summary HAVING COUNT(*) > 1"
        ") AS d "
        "WHERE m.summary = d.summary AND m.app_id = $1 AND m.user_id = $2 "
        "AND m.memory_layer = 2 AND m.status = 1 AND m.id < d.keep_id",
        str(app_id), str(user_id),
    )
    return _affected(tag)


# ── 衰减 / 归档（P0-11：hot 豁免，只作用于 warm）──────────────────────────────

async def decay_warm_soft_delete(days: int, batch: int = 5000) -> int:
    """30 天未命中软删：status=4 + active_slot=NULL。

    PG 的 UPDATE 不支持 LIMIT，用子查询先取一批 id 再更新，避免长时间持锁。
    """
    tag = await memory_execute(
        "UPDATE agent.agent_memory SET status = 4, active_slot = NULL "
        "WHERE id IN ("
        "  SELECT id FROM agent.agent_memory "
        "  WHERE memory_layer = 2 AND status = 1 "
        "  AND COALESCE(last_hit_at, created_at) < now() - make_interval(days => $1) "
        "  ORDER BY id LIMIT $2"
        ")",
        int(days), int(batch),
    )
    return _affected(tag)


async def scan_archivable(days: int, limit: int = 2000) -> list[MemoryEntry]:
    """归档候选：status=4 且软删后再满 N 天，valid_to IS NULL 表示尚未导出。"""
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS} FROM agent.agent_memory "
        "WHERE memory_layer = 2 AND status = 4 AND valid_to IS NULL "
        "AND updated_at < now() - make_interval(days => $1) "
        "ORDER BY id LIMIT $2",
        int(days), int(limit),
    )
    return _rows_to_entries(rows)


async def mark_archived_exported(ids: list[int]) -> None:
    """归档导出完成：valid_to=now()（生效截止，后续归档扫描跳过）。"""
    if not ids:
        return
    await memory_execute(
        "UPDATE agent.agent_memory SET valid_to = now() WHERE id = ANY($1::bigint[])",
        [int(i) for i in ids],
    )


async def list_tenants() -> list[tuple[str, str]]:
    """枚举有记忆数据的 (app_id, user_id)。"""
    rows = await memory_fetch("SELECT DISTINCT app_id, user_id FROM agent.agent_memory")
    return [(str(r[0]), str(r[1])) for r in rows]


# ── 合规删除（P0-10，设计 §6.4）───────────────────────────────────────────────

async def find_for_compliance_delete(
    app_id: str, user_id: str, scope: str, target: str
) -> list[tuple[int, str]]:
    """按范围定位待删除记忆，返回 [(id, memory_id), ...]。"""
    if scope == "all":
        where, extra = "", []
    elif scope == "memory_id":
        where, extra = " AND memory_id = $3", [target]
    elif scope == "subject":
        where, extra = " AND subject = $3", [target]
    elif scope == "source_msg_id":
        # source_msg_ids 为 JSONB 数组，@> 判断是否包含该消息 uid（可走 GIN）
        where, extra = " AND source_msg_ids @> jsonb_build_array($3::text)", [target]
    else:
        raise ValueError(f"未知合规删除范围: {scope}")
    rows = await memory_fetch(
        "SELECT id, memory_id FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2" + where,
        str(app_id), str(user_id), *extra,
    )
    return [(int(r[0]), str(r[1])) for r in rows]


async def compliance_hard_delete(app_id: str, user_id: str, ids: list[int]) -> int:
    """监管要求路径：物理硬删（向量随行删除，无需二次清理外部索引）。"""
    if not ids:
        return 0
    tag = await memory_execute(
        "DELETE FROM agent.agent_memory WHERE id = ANY($1::bigint[]) AND app_id = $2 AND user_id = $3",
        [int(i) for i in ids], str(app_id), str(user_id),
    )
    from codegenx.ai_service.memory import hot_cache
    await hot_cache.invalidate(str(app_id), str(user_id))
    return _affected(tag)


async def compliance_soft_delete_redact(app_id: str, user_id: str, ids: list[int]) -> int:
    """业务留痕路径：status=3 + 内容脱敏（summary/content 置固定占位）。

    active_slot 必须同语句置 NULL（槽位释放）；向量保留但行已非 active，
    partial HNSW 索引（只含 status=1）自动把它排除出召回。
    """
    if not ids:
        return 0
    redacted = json.dumps({"text": "[已删除]", "topic": ""}, ensure_ascii=False)
    tag = await memory_execute(
        "UPDATE agent.agent_memory "
        "SET status = 3, active_slot = NULL, summary = '[已删除]', content = $1::jsonb "
        "WHERE id = ANY($2::bigint[]) AND app_id = $3 AND user_id = $4",
        redacted, [int(i) for i in ids], str(app_id), str(user_id),
    )
    from codegenx.ai_service.memory import hot_cache
    await hot_cache.invalidate(str(app_id), str(user_id))
    return _affected(tag)


async def count_active(app_id: str, user_id: str) -> int:
    """删除校验用：该用户 active 记忆数（合规删除后应为 0，scope=all 时）。"""
    val = await memory_fetchval(
        "SELECT COUNT(*) FROM agent.agent_memory WHERE app_id = $1 AND user_id = $2 AND status = 1",
        str(app_id), str(user_id),
    )
    return int(val or 0)


# ── 管理/排查查询（P2-7 排查工具五件套 + P2-5 管理后台）────────────────────────

async def get_by_memory_ids(
    app_id: str, user_id: str, memory_ids: list[str]
) -> list[MemoryEntry]:
    """按 memory_id 批量取完整行（uk 唯一；租户过滤防越权）。"""
    if not memory_ids:
        return []
    rows = await memory_fetch(
        f"SELECT {_ROW_COLS} FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2 AND memory_id = ANY($3::text[])",
        str(app_id), str(user_id), list(memory_ids),
    )
    return _rows_to_entries(rows)


async def list_memories(
    app_id: str,
    user_id: str,
    *,
    status: int = 1,
    memory_layer: int | None = None,
    memory_type: str | None = None,
    limit: int = 200,
) -> list[MemoryEntry]:
    """按租户列出记忆（P2-7 ① 全貌查询；status≤0 表示不过滤状态看全部）。"""
    sql = f"SELECT {_ROW_COLS} FROM agent.agent_memory WHERE app_id = $1 AND user_id = $2"
    args: list = [str(app_id), str(user_id)]
    if status and int(status) > 0:
        args.append(int(status))
        sql += f" AND status = ${len(args)}"
    if memory_layer:
        args.append(int(memory_layer))
        sql += f" AND memory_layer = ${len(args)}"
    if memory_type:
        args.append(str(memory_type))
        sql += f" AND memory_type = ${len(args)}"
    args.append(int(limit))
    sql += f" ORDER BY memory_layer ASC, updated_at DESC LIMIT ${len(args)}"
    rows = await memory_fetch(sql, *args)
    return _rows_to_entries(rows)


# ── 向量重建（原 Qdrant 重建入口的等价实现）────────────────────────────────────

async def count_warm_with_vector(app_id: str, user_id: str) -> int:
    """该租户 active warm 中已有向量的行数（重建后校验用）。"""
    val = await memory_fetchval(
        "SELECT COUNT(*) FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2 AND memory_layer = 2 AND status = 1 "
        "AND embedding IS NOT NULL",
        str(app_id), str(user_id),
    )
    return int(val or 0)


async def rebuild_warm_embeddings(app_id: str, user_id: str) -> tuple[int, int]:
    """单用户向量重建：重新 embed 全部 active warm 行并原地更新 embedding。

    返回 (待重建行数, 已重建行数)。embedding 模型/维度变更后调用。
    """
    from codegenx.ai_service.llm.embedding import get_embedding_client
    rows = await memory_fetch(
        "SELECT id, summary, content FROM agent.agent_memory "
        "WHERE app_id = $1 AND user_id = $2 AND memory_layer = 2 AND status = 1",
        str(app_id), str(user_id),
    )
    if not rows:
        return 0, 0
    texts = []
    for r in rows:
        raw = r["content"]
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            body = str(parsed.get("text", "") or "") if isinstance(parsed, dict) else str(parsed)
        except (json.JSONDecodeError, TypeError):
            body = ""
        texts.append(str(r["summary"] or "") or body)
    vectors = await get_embedding_client().embed_texts(texts)
    done = 0
    async with memory_transaction() as conn:
        for r, vec in zip(rows, vectors):
            await conn.execute(
                "UPDATE agent.agent_memory SET embedding = $1::text::vector WHERE id = $2",
                vector_text(vec), int(r["id"]),
            )
            done += 1
    return len(rows), done
