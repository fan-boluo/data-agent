"""
合规删除级联（P0-10，设计方案 v2.1 §6.4）——《个保法》要求可删除且派生数据一并清理。

记忆系统是「派生数据放大器」：一条消息可能派生多条记忆、多个向量、多份归档。
删除范围（scope）：
  all            该用户全部记忆
  memory_id      单条记忆
  subject        某主题（subject 列）
  source_msg_id  某条来源消息的派生记忆（source_msg_ids JSON 反查——
                 没有该字段本流程无法从「某条消息」找到派生记忆）

级联顺序：PostgreSQL（真源：硬删=监管要求 / 软删+脱敏=业务留痕，§6.4 ②）
  → 向量随行处理（pgvector 同表，删行/失效即对召回生效，§6.4 ③）
  → 校验兜底（count_active 复核；单库单表无「外部索引幽灵」一类残留）
Redis hot 缓存（P1-1）与对象存储归档接入后，须在此补 DEL / 归档清理步骤。

审计：结构化日志（§10.2 硬要求——日志不含记忆正文，脱敏靠 memory_id 回查）。
"""
from __future__ import annotations

import json

from shared import log
from shared.constants import DATA_ROOT_DIR
from codegenx.ai_service.memory.models import new_ulid
from codegenx.ai_service.memory.memory_store import (
    find_for_compliance_delete,
    compliance_hard_delete,
    compliance_soft_delete_redact,
    count_active,
)

VALID_SCOPES = ("all", "memory_id", "subject", "source_msg_id")


async def run_compliance_delete(
    app_id: str,
    user_id: str,
    scope: str = "all",
    target: str = "",
    hard: bool = True,
    request_id: str = "",
) -> int:
    """执行一次级联删除，返回 MySQL 侧删除行数。

    Args:
        scope: all / memory_id / subject / source_msg_id
        target: scope 非 all 时的定位值
        hard: True=物理硬删；False=软删 + 内容脱敏（B7 待法务意见，默认硬删）
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"未知合规删除范围: {scope}（可选 {VALID_SCOPES}）")
    if scope != "all" and not target:
        raise ValueError(f"scope={scope} 需要提供 target")

    rows = await find_for_compliance_delete(app_id, user_id, scope, target)
    if not rows:
        log.info("[compliance_delete] 无匹配记忆 app={} user={} scope={} target={}",
                 app_id, user_id, scope, target)
        return 0
    ids = [r[0] for r in rows]
    memory_ids = [r[1] for r in rows]

    if hard:
        deleted = await compliance_hard_delete(app_id, user_id, ids)
    else:
        deleted = await compliance_soft_delete_redact(app_id, user_id, ids)

    # 向量与记忆同表（pgvector）：行已删除/失效即对召回生效，无需外部索引清理

    # 校验（§6.4 ⑦）：scope=all 时 active 应为 0；异步兜底由每日对账承担
    if scope == "all":
        remaining = await count_active(app_id, user_id)
        if remaining > 0:
            log.error("[compliance_delete] 删除校验失败: app={} user={} 仍剩 {} 条 active",
                      app_id, user_id, remaining)

    # 审计日志：谁/何时/删了什么范围/依据什么——不含任何记忆正文
    log.info(
        "[compliance_delete] {} app={} user={} scope={} target={} rows={} request_id={} archive_dir_hint={}",
        "HARD" if hard else "SOFT_REDACT",
        app_id, user_id, scope, target, deleted, request_id,
        str(DATA_ROOT_DIR / str(user_id) / str(app_id) / "memory" / "archive"),
    )
    return deleted


async def enqueue_compliance_delete(
    app_id: str,
    user_id: str,
    scope: str = "all",
    target: str = "",
    hard: bool = True,
    request_id: str | None = None,
) -> int | None:
    """登记 compliance_delete 离线任务（幂等键 {app_id}:{user_id}:{request_id}）。

    管理接口/客服工单触发时调用；同 request_id 重复提交返回 None。
    """
    from codegenx.ai_service.schedule.memory_task_store import (
        get_memory_task_store,
        TASK_COMPLIANCE_DELETE,
    )

    if scope not in VALID_SCOPES:
        raise ValueError(f"未知合规删除范围: {scope}")
    request_id = request_id or new_ulid()
    task_id = await get_memory_task_store().enqueue(
        TASK_COMPLIANCE_DELETE,
        app_id=app_id,
        user_id=user_id,
        payload={"scope": scope, "target": target, "hard": bool(hard), "request_id": request_id},
        idempotency_key=f"{app_id}:{user_id}:{request_id}",
    )
    if task_id is None:
        log.warning("[compliance_delete] 幂等命中，任务已存在: app={} user={} request_id={}",
                    app_id, user_id, request_id)
    return task_id


def audit_summary(request_id: str, scope: str, deleted: int) -> str:
    """审计摘要素列化（供工单/后台展示；不含正文）。"""
    return json.dumps(
        {"request_id": request_id, "scope": scope, "deleted": deleted},
        ensure_ascii=False,
    )
