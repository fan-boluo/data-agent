"""记忆管理后台 / 排查工具路由（P2-7 五件套 + P2-5 最小管理端，设计方案 §10.3/附录 A）。

挂载 /api/admin/memory/*，router 级收紧为管理员（对齐 monitor_router 先例）。
排查五件套（§10.3）：
  ① GET  /list                    按租户列出记忆全貌
  ② GET  /{memory_id}/trace       溯源：source_msg_ids → chat_message 原始消息
  ③ GET  /task/{task_id}          提炼批次：任务行 + 消费区间消息 + 产出记忆
  ④ POST /retrieval-sim           模拟检索：双路原始分 + 逐因子重排明细（不登记命中）
  ⑤ POST /rebuild-vectors         单用户向量重建：对 active warm 行原地重新 embed
管理后台（附录 A：PostgreSQL 是真源，一切修改走接口——手改库不会失效
Redis hot 缓存、也不会重建 warm 向量，这正是本路由存在的理由）：
  ⑥ POST /deactivate              停用（status=3 撤销，走 mark_inactive 硬约束）
  ⑦ POST /update                  编辑（新条取代旧条：source_type=3 人工录入、confidence=1.0）

审计：全部写操作打 metrics.log_event 结构化日志（仅元数据，不含记忆正文）。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from codegenx.ai_service.chat_message import get_chat_message_store
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.memory.memory_store import (
    append_warm_embedded,
    get_by_memory_ids,
    list_memories,
    mark_inactive,
    rebuild_warm_embeddings,
    count_warm_with_vector,
    upsert_hot_slot,
)
from codegenx.ai_service.memory.models import (
    BUILTIN_MEMORY_TYPES,
    LAYER_HOT,
    SOURCE_MANUAL,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    STATUS_REVOKED,
    MemoryEntry,
    estimate_text_tokens,
    layer_of,
)
from codegenx.ai_service.memory.retriever import debug_recall
from codegenx.ai_service.memory.writer import match_sensitive
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.schedule.memory_task_store import (
    TASK_WARM_EXTRACT,
    get_memory_task_store,
)
from codegenx.gateway.middleware.auth import require_role
from codegenx.gateway.middleware.jwt_auth import JWTUser
from codegenx.user_service.user_enums import UserRole
from shared.exceptions.business_exception import BusinessException
from shared.exceptions.error_code import ErrorCode
from shared.utils.result_utils import success

memory_admin_router = APIRouter(
    prefix="/admin/memory",
    tags=["memory-admin"],
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)


def _require_tenant(app_id: str, user_id: str) -> None:
    if not str(app_id or "").strip() or not str(user_id or "").strip():
        raise BusinessException(ErrorCode.PARAMS_ERROR, "app_id/user_id 不能为空")


def _entry_view(e: MemoryEntry) -> dict:
    """MemoryEntry → 排查视图（dataclass 全字段；content 为原文，管理员可见）。"""
    return asdict(e)


# ── ① 租户记忆全貌 ──────────────────────────────────────────────────────────────

@memory_admin_router.get("/list")
async def admin_list_memories(
    app_id: str,
    user_id: str,
    status: int = Query(default=1, ge=0, le=4),
    memory_type: str | None = Query(default=None, alias="memoryType"),
    memory_layer: int | None = Query(default=None, alias="memoryLayer", ge=0, le=2),
    limit: int = Query(default=200, ge=1, le=500),
):
    """列出租户记忆（status=0 不过滤状态，看含失效在内的全量历史）。"""
    _require_tenant(app_id, user_id)
    entries = await list_memories(
        app_id, user_id, status=status, memory_layer=memory_layer,
        memory_type=memory_type, limit=limit,
    )
    return success([_entry_view(e) for e in entries])


# ── ② 记忆溯源（source_msg_ids → 原始消息） ─────────────────────────────────────

@memory_admin_router.get("/{memory_id}/trace")
async def admin_trace_memory(memory_id: str, app_id: str, user_id: str):
    """回答「这条记忆从哪来」：memory_id → source_msg_ids → chat_message 原文。

    注：P2 之前产生的记忆 source_msg_ids 为空（当时链路未写入），只能看到
    session_id/task_id 级线索；新记忆自带逐条消息溯源。
    """
    _require_tenant(app_id, user_id)
    if not str(memory_id or "").strip():
        raise BusinessException(ErrorCode.PARAMS_ERROR, "memory_id 不能为空")
    entries = await get_by_memory_ids(app_id, user_id, [memory_id])
    if not entries:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "记忆不存在（或不在该租户下）")
    view = _entry_view(entries[0])
    uids = [str(u) for u in (entries[0].source_msg_ids or [])]
    messages = await get_chat_message_store().get_by_uids(uids) if uids else []
    found = {m["message_uid"] for m in messages}
    view["source_messages"] = messages
    view["source_missing_uids"] = [u for u in uids if u not in found]  # 已被保留期清理
    return success(view)


# ── ③ 提炼批次上下文 ───────────────────────────────────────────────────────────

@memory_admin_router.get("/task/{task_id}")
async def admin_inspect_task(task_id: int, message_preview_chars: int = Query(default=200, alias="previewChars", ge=20, le=2000)):
    """回答「这批记忆怎么提炼出来的」：任务行 + 消费区间消息 + 产出记忆。

    消费区间来自任务审计 payload（from_seq/consumed_seq，P1 起记录）；更早的
    任务无 from_seq 时降级为「consumed_seq 前 N 条」近似展示。
    """
    task = await get_memory_task_store().get_by_id(task_id)
    if task is None:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "任务不存在")
    view: dict = {
        "id": task.get("id"),
        "task_type": task.get("task_type"),
        "app_id": task.get("app_id"),
        "user_id": task.get("user_id"),
        "session_id": task.get("session_id"),
        "status": task.get("status"),
        "retry_count": task.get("retry_count"),
        "produced_count": task.get("produced_count"),
        "last_error": task.get("last_error"),
        "payload": task.get("payload"),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
    }

    payload = task.get("payload") or {}
    session_id = str(task.get("session_id") or "")

    # 消费区间消息（正文截断预览，排查够用且防响应爆炸）
    consumed_seq = int(payload.get("consumed_seq") or 0)
    if session_id and consumed_seq > 0:
        from_seq = int(payload.get("from_seq") or 0)
        limit = max(1, consumed_seq - from_seq)
        records = await get_chat_message_store().read_since(
            session_id, after_seq=from_seq, limit=limit
        )
        view["consumed_messages"] = [
            {
                "seq": seq,
                "message_uid": m.get("message_uid", ""),
                "role": m.get("role", ""),
                "content_preview": str(m.get("content") or "")[:message_preview_chars],
            }
            for seq, m in records
            if seq <= consumed_seq
        ]

    # 产出记忆（payload.memory_ids → 完整行）
    memory_ids = [str(m) for m in (payload.get("memory_ids") or []) if m]
    if memory_ids:
        produced = await get_by_memory_ids(
            str(task.get("app_id") or ""), str(task.get("user_id") or ""), memory_ids
        )
        view["produced_memories"] = [
            {
                "memory_id": e.memory_id,
                "memory_layer": e.memory_layer,
                "memory_type": e.memory_type,
                "summary": e.summary,
                "status": e.status,
                "task_id": e.task_id,
            }
            for e in produced
        ]
    return success(view)


# ── ④ 模拟检索 ─────────────────────────────────────────────────────────────────

class RetrievalSimRequest(BaseModel):
    app_id: str
    user_id: str
    query: str


@memory_admin_router.post("/retrieval-sim")
async def admin_retrieval_sim(req: RetrievalSimRequest):
    """回答「为什么召回了这条 / 为什么没召回那条」：双路原始分 + 重排因子明细。

    模拟不落命中（hit 统计不受污染）；candidates 逐条标注来源通道
    （vector / keyword / both）与各分项得分，便于定位「该召回却没召回」。
    """
    _require_tenant(req.app_id, req.user_id)
    if not str(req.query or "").strip():
        raise BusinessException(ErrorCode.PARAMS_ERROR, "query 不能为空")
    return success(await debug_recall(req.app_id, req.user_id, req.query))


# ── ⑤ 单用户向量重建 ───────────────────────────────────────────────────────────

class RebuildVectorsRequest(BaseModel):
    app_id: str
    user_id: str


@memory_admin_router.post("/rebuild-vectors")
async def admin_rebuild_vectors(
    req: RebuildVectorsRequest, admin_user: JWTUser = Depends(require_role(UserRole.ADMIN))
):
    """索引损坏 / embedding 换模型后的单用户重建（§10.3 ⑤）。

    pgvector 与记忆同表：直接对该租户全部 active warm 行重新 embed 并原地更新，
    无需「清点位 + 置 NULL + 异步 Worker 重灌」三步。
    """
    _require_tenant(req.app_id, req.user_id)
    total, rebuilt = await rebuild_warm_embeddings(req.app_id, req.user_id)
    remaining = await count_warm_with_vector(req.app_id, req.user_id)
    metrics.log_event(
        "admin_rebuild_vectors", admin_id=str(admin_user.user_id),
        app_id=req.app_id, user_id=req.user_id, total_rows=total, rebuilt_rows=rebuilt,
    )
    return success({
        "total_rows": total,
        "rebuilt_rows": rebuilt,
        "remaining_points": remaining,
        "note": "已在原表原地重建向量，可用 /retrieval-sim 立即复验",
    })


# ── ⑥ 停用（P2-5） ─────────────────────────────────────────────────────────────

class DeactivateMemoryRequest(BaseModel):
    app_id: str
    user_id: str
    memory_id: str
    reason: str = ""


@memory_admin_router.post("/deactivate")
async def admin_deactivate_memory(
    req: DeactivateMemoryRequest, admin_user: JWTUser = Depends(require_role(UserRole.ADMIN))
):
    """停用一条记忆（status=3 撤销）。

    走 mark_inactive 硬约束（同句清 active_slot + 重置 vector_synced_at）；
    warm 点位物理删除由每日 sync_check 幽灵清理兜底（与 conflict_detect 同策略）。
    """
    _require_tenant(req.app_id, req.user_id)
    if not str(req.memory_id or "").strip():
        raise BusinessException(ErrorCode.PARAMS_ERROR, "memory_id 不能为空")
    entries = await get_by_memory_ids(req.app_id, req.user_id, [req.memory_id])
    if not entries:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "记忆不存在（或不在该租户下）")
    entry = entries[0]
    if entry.status != STATUS_ACTIVE:
        return success({"deactivated": False, "status": entry.status, "note": "已是非 active 状态"})
    changed = await mark_inactive(req.app_id, req.user_id, [req.memory_id], STATUS_REVOKED)
    metrics.log_event(
        "admin_memory_deactivate", admin_id=str(admin_user.user_id),
        app_id=req.app_id, user_id=req.user_id, memory_id=req.memory_id,
        memory_layer=entry.memory_layer, memory_type=entry.memory_type,
        reason=str(req.reason or "")[:200],
    )
    return success({"deactivated": bool(changed)})


# ── ⑦ 编辑（新条取代旧条，P2-5 / 附录 A 管理接口规约） ─────────────────────────

class UpdateMemoryRequest(BaseModel):
    app_id: str
    user_id: str
    memory_id: str
    summary: str | None = None   # 不传则取 content 前 500 字（与提炼链路口径一致）
    content: str | None = None
    topic: str | None = None
    memory_type: str | None = None  # 换类型则层归属随之变化
    slot_key: str | None = None     # 仅 hot 层有意义；默认沿用旧槽位


@memory_admin_router.post("/update")
async def admin_update_memory(
    req: UpdateMemoryRequest, admin_user: JWTUser = Depends(require_role(UserRole.ADMIN))
):
    """编辑记忆 = 生成新条取代旧条（不可变历史，附录 A 规约）：

    source_type=3（人工录入）、confidence=1.00；hot 走 uk_slot upsert
    （写时 DEL 缓存由 DAO 内置），warm 追加且 vector_synced_at=NULL 自动
    入同步队列（30s 内向量层跟上）；旧条 superseded_by 指向新条；
    source_msg_ids 溯源链保留。敏感信息拦截与提炼链路同标准。
    """
    _require_tenant(req.app_id, req.user_id)
    if not str(req.memory_id or "").strip():
        raise BusinessException(ErrorCode.PARAMS_ERROR, "memory_id 不能为空")
    rows = await get_by_memory_ids(req.app_id, req.user_id, [req.memory_id])
    if not rows:
        raise BusinessException(ErrorCode.NOT_FOUND_ERROR, "记忆不存在（或不在该租户下）")
    old = rows[0]

    content = str(req.content if req.content is not None else old.content or "").strip()
    if not content:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "content 不能为空")
    hit = match_sensitive(content)
    if hit:
        raise BusinessException(ErrorCode.PARAMS_ERROR, f"内容含敏感信息({hit})，拒绝写入")
    memory_type = str(req.memory_type or old.memory_type or "").strip()
    if memory_type not in BUILTIN_MEMORY_TYPES:
        raise BusinessException(ErrorCode.PARAMS_ERROR, f"未知记忆类型: {memory_type}")
    topic = str(req.topic if req.topic is not None else old.topic or "").strip()
    summary = str(req.summary if req.summary is not None else content or "").strip() or content[:500]

    new_entry = MemoryEntry(
        app_id=str(req.app_id),
        user_id=str(req.user_id),
        memory_type=memory_type,
        subject=old.subject,
        slot_key=str(req.slot_key if req.slot_key is not None else old.slot_key or "").strip()[:64],
        summary=summary[:500],
        content=content,
        topic=topic[:32],
        token_cost=estimate_text_tokens(content) + 4,
        source_type=SOURCE_MANUAL,
        confidence=1.00,
        session_id=old.session_id,
        source_msg_ids=list(old.source_msg_ids or []),
    )
    layer = layer_of(memory_type)
    if layer == LAYER_HOT:
        hot_max = int(getattr(config.memory.store, "hot_max_entries", 80) or 80)
        new_id = await upsert_hot_slot(
            req.app_id, req.user_id, new_entry, hot_max_entries=hot_max
        )
        if not new_id:
            raise BusinessException(
                ErrorCode.SYSTEM_ERROR,
                f"hot 层已满（上限 {hot_max}），请等待 consolidate 压缩或调大 hotMaxEntries",
            )
    else:
        new_id = await append_warm_embedded(req.app_id, req.user_id, new_entry)
        new_entry.id = new_id

    # 旧条失效指向新条（放新条写成功之后，失败窗口内最多少一条新条，不会丢记忆）
    await mark_inactive(
        req.app_id, req.user_id, [old.memory_id],
        STATUS_SUPERSEDED, superseded_by=new_entry.memory_id,
    )
    metrics.log_event(
        "admin_memory_update", admin_id=str(admin_user.user_id),
        app_id=req.app_id, user_id=req.user_id,
        old_memory_id=old.memory_id, new_memory_id=new_entry.memory_id,
        old_type=old.memory_type, new_type=memory_type, new_layer=layer,
    )
    return success({
        "old_memory_id": old.memory_id,
        "new_memory_id": new_entry.memory_id,
        "memory_layer": layer,
        "note": "warm 层向量将在 30s 内由同步 Worker 跟进；hot 层缓存已即时失效",
    })
