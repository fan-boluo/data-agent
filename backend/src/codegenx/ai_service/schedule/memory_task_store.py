"""
记忆离线任务队列 + 消费水位 —— PostgreSQL DAO（asyncpg 原生）。

对应表（DDL 见 init/pg_memory_schema.sql，由部署方手动执行，本模块不建表）：
  memory.memory_task      离线任务队列（idempotency_key 幂等 + produced_count 审计）
  memory.memory_watermark  会话消息消费水位（chat_message.seq，会话内单调递增）

状态机：pending → running → done
                └→ 失败：retry_count+1，按退避(1m/5m/30m)回 pending；超限 → dead
语义：at-least-once。任务失败重跑可能重复写记忆：
  hot  由 uk_memory_slot upsert 天然幂等（同槽位覆盖）
  warm 由 memory_task 幂等键/at-least-once + 后续 conflict_detect 兜底

说明：向量与记忆行同表同事务写入（pgvector），双数据源对账子系统整体下线。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from db.postgres.postgres_client import (
    memory_execute,
    memory_fetch,
    memory_fetchrow,
    memory_fetchval,
    memory_transaction,
)
from shared import log as logger

# 任务类型常量（与 pg_memory_schema.sql 的注释一致）
TASK_WARM_EXTRACT = "warm_extract"
TASK_CONSOLIDATE = "consolidate"
TASK_DECAY_ARCHIVE = "decay_archive"
TASK_COMPLIANCE_DELETE = "compliance_delete"
TASK_CONFLICT_DETECT = "conflict_detect"  # 每日异步矛盾检测

# 失败退避序列（秒）：第 n 次失败后等待 backoffs[n-1] 再重试
RETRY_BACKOFFS = (60.0, 300.0, 1800.0)
MAX_RETRY = len(RETRY_BACKOFFS)


class MemoryTaskStore:
    """memory_task / memory_watermark 两表 DAO（PostgreSQL）。"""

    # === 任务登记 ===

    async def enqueue(
        self,
        task_type: str,
        app_id: str,
        session_id: str = "",
        user_id: str = "",
        payload: dict | None = None,
        next_run_at: float = 0.0,
        dedup: bool = False,
        idempotency_key: str | None = None,
    ) -> int | None:
        """登记一个离线任务，返回任务 id。

        - dedup=True：同类型同会话已有 pending/running 任务则跳过
          （聊天在途高频触发靠它防任务爆炸）
        - idempotency_key：uk_memory_task_idempotency 唯一约束兜底（合规删除等
          强幂等场景）；键冲突返回 None
        """
        if dedup:
            hit = await memory_fetchval(
                "SELECT id FROM memory.memory_task "
                "WHERE task_type = $1 AND session_id = $2 AND status IN ('pending','running') "
                "LIMIT 1",
                task_type, session_id,
            )
            if hit is not None:
                return None
        # 幂等键冲突时不插入，RETURNING 无行 → 返回 None
        new_id = await memory_fetchval(
            "INSERT INTO memory.memory_task "
            "(task_type, idempotency_key, app_id, user_id, session_id, status, payload, next_run_at) "
            "VALUES ($1, $2, $3, $4, $5, 'pending', $6::jsonb, $7) "
            "ON CONFLICT (idempotency_key) DO NOTHING "
            "RETURNING id",
            task_type,
            idempotency_key,
            str(app_id),
            str(user_id or ""),
            session_id,
            json.dumps(payload, ensure_ascii=False) if payload else None,
            int(next_run_at),
        )
        return int(new_id) if new_id is not None else None

    # === 任务领取与状态流转 ===

    async def claim_due(self, limit: int = 5, now: float | None = None) -> list[dict[str, Any]]:
        """原子领取到期 pending 任务并置 running。

        单事务内 SELECT ... FOR UPDATE SKIP LOCKED + UPDATE：
        多消费者也不会把同一任务发两次（当前为单消费者，双保险）。
        """
        now = now or time.time()
        async with memory_transaction() as conn:
            rows = await conn.fetch(
                "SELECT * FROM memory.memory_task "
                "WHERE status = 'pending' AND next_run_at <= $1 "
                "ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED",
                int(now), int(limit),
            )
            tasks = [dict(r) for r in rows]
            if tasks:
                ids = [int(t["id"]) for t in tasks]
                await conn.execute(
                    "UPDATE memory.memory_task SET status = 'running' WHERE id = ANY($1::bigint[])",
                    ids,
                )
                for t in tasks:
                    t["status"] = "running"
        for t in tasks:
            t["payload"] = self._parse_payload(t.get("payload"))
        return tasks

    async def mark_done(
        self, task_id: int, produced_count: int = 0, result_payload: dict | None = None
    ) -> None:
        """完成（produced_count 审计 + payload 执行结果回填）。"""
        if result_payload:
            await memory_execute(
                "UPDATE memory.memory_task SET status = 'done', produced_count = $2, "
                "last_error = NULL, "
                "payload = COALESCE(payload, '{}'::jsonb) || $3::jsonb "
                "WHERE id = $1",
                int(task_id), int(produced_count),
                json.dumps(result_payload, ensure_ascii=False, default=str),
            )
        else:
            await memory_execute(
                "UPDATE memory.memory_task SET status = 'done', produced_count = $2, "
                "last_error = NULL WHERE id = $1",
                int(task_id), int(produced_count),
            )

    async def reschedule(self, task_id: int, delay_sec: float = 60.0) -> None:
        """无过错重排（如会话锁被占）：保持 pending，延后领取，不计失败次数。"""
        await memory_execute(
            "UPDATE memory.memory_task SET status = 'pending', next_run_at = $2 WHERE id = $1",
            int(task_id), int(time.time() + delay_sec),
        )

    async def touch_running(self, task_id: int) -> None:
        """长任务心跳：仅刷新 updated_at，防止被卡死回收巡检误杀。

        updated_at 由触发器 trg_memory_task_touch 在 UPDATE 时刷新，
        这里显式赋值是为了语义清晰（本语句只做心跳，不改业务字段）。
        """
        await memory_execute(
            "UPDATE memory.memory_task SET updated_at = now() "
            "WHERE id = $1 AND status = 'running'",
            int(task_id),
        )

    async def reclaim_stuck(self, timeout_sec: int = 600) -> int:
        """卡死回收：running 且 updated_at 超时的任务复位 pending。

        Worker 崩溃会让任务永久停在 running；巡检每分钟跑一次。
        返回复位行数（指标 memory.task.stuck_reclaimed）。
        """
        rows = await memory_fetch(
            "UPDATE memory.memory_task SET status = 'pending', next_run_at = $1 "
            "WHERE status = 'running' "
            "AND updated_at < now() - make_interval(secs => $2::int) "
            "RETURNING id",
            int(time.time()), int(timeout_sec),
        )
        return len(rows)

    async def mark_failed(self, task_id: int, error: str, backoffs: tuple = RETRY_BACKOFFS) -> str:
        """失败退避：回 pending 延迟重试；超限置 dead。返回处理后的状态。"""
        now = time.time()
        prev = await memory_fetchval(
            "SELECT retry_count FROM memory.memory_task WHERE id = $1", int(task_id)
        )
        retry = (int(prev or 0)) + 1
        if retry > len(backoffs):
            status, next_at = "dead", now
        else:
            status, next_at = "pending", now + backoffs[retry - 1]
        await memory_execute(
            "UPDATE memory.memory_task SET status = $1, retry_count = $2, "
            "next_run_at = $3, last_error = $4 WHERE id = $5",
            status, retry, int(next_at), error[:2000], int(task_id),
        )
        return status

    async def recover_running(self) -> int:
        """启动/关闭时把 running 任务复位 pending（崩溃恢复，at-least-once）。"""
        rows = await memory_fetch(
            "UPDATE memory.memory_task SET status = 'pending' "
            "WHERE status = 'running' RETURNING id"
        )
        changed = len(rows)
        if changed:
            logger.info("记忆任务崩溃恢复: {} 个 running 任务已复位为 pending", changed)
        return changed

    # === 统计与调度辅助 ===

    async def stats(self) -> dict[str, int]:
        rows = await memory_fetch(
            "SELECT status, COUNT(*) AS c FROM memory.memory_task GROUP BY status"
        )
        return {str(r["status"]): int(r["c"]) for r in rows}

    async def get_by_id(self, task_id: int) -> dict | None:
        """按 id 取任务完整行（提炼批次排查）。payload 解析为 dict。"""
        row = await memory_fetchrow(
            "SELECT * FROM memory.memory_task WHERE id = $1", int(task_id)
        )
        if row is None:
            return None
        task = dict(row)
        task["payload"] = self._parse_payload(task.get("payload"))
        return task

    async def consolidate_scheduled_today(self) -> bool:
        """今天是否已登记过 consolidate（每日定时调度的判重依据）。"""
        return await self._scheduled_today("consolidate")

    async def decay_scheduled_today(self) -> bool:
        """今天是否已登记过 decay_archive。"""
        return await self._scheduled_today("decay_archive")

    async def conflict_scheduled_today(self) -> bool:
        """今天是否已登记过 conflict_detect。"""
        return await self._scheduled_today("conflict_detect")

    async def _scheduled_today(self, task_type: str) -> bool:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        hit = await memory_fetchval(
            "SELECT id FROM memory.memory_task WHERE task_type = $1 "
            "AND status IN ('pending','running','done') AND created_at >= $2 LIMIT 1",
            task_type, today_start,
        )
        return hit is not None

    # === 水位（memory.memory_watermark 表）===

    async def get_watermark(self, session_id: str) -> int:
        """读取会话消费水位（已消费的最大 chat_message.seq）；无记录返回 0。"""
        val = await memory_fetchval(
            "SELECT last_seq FROM memory.memory_watermark WHERE session_id = $1", session_id
        )
        return int(val or 0)

    async def get_watermark_full(self, session_id: str) -> dict | None:
        """读取完整水位状态（漏斗触发判断用）。"""
        row = await memory_fetchrow(
            "SELECT last_seq, pending_signals, pending_tokens, last_extract_at "
            "FROM memory.memory_watermark WHERE session_id = $1",
            session_id,
        )
        return dict(row) if row else None

    async def bump_pending(
        self, session_id: str, app_id: str, user_id: str,
        signals: int, tokens: int,
    ) -> dict | None:
        """第一级：累加轻量信号计数（upsert 原子自增），返回累加后状态。

        只动 pending_* 计数，不碰 last_seq（消费位点唯一写者是提炼 Worker）。
        """
        await memory_execute(
            "INSERT INTO memory.memory_watermark "
            "(session_id, app_id, user_id, last_seq, pending_signals, pending_tokens) "
            "VALUES ($1, $2, $3, 0, $4, $5) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "pending_signals = memory.memory_watermark.pending_signals + EXCLUDED.pending_signals, "
            "pending_tokens = memory.memory_watermark.pending_tokens + EXCLUDED.pending_tokens",
            session_id, str(app_id), str(user_id or ""),
            max(0, int(signals)), max(0, int(tokens)),
        )
        return await self.get_watermark_full(session_id)

    async def clear_pending(self, session_id: str) -> None:
        """清零漏斗计数（no-op 提取也须清账，否则 patrol 重复补投空转）。

        兜底补投是「投递前崩溃」一次性补投：补投过即应退场。半截轮次场景安全：
        后续轮次完成时漏斗会重新累加并投递。
        updated_at 由触发器自动刷新，本表随之脱离 stale 扫描集。
        """
        await memory_execute(
            "UPDATE memory.memory_watermark "
            "SET pending_signals = 0, pending_tokens = 0 "
            "WHERE session_id = $1",
            session_id,
        )

    async def scan_stale_pending(self, minutes: int, limit: int = 50) -> list[dict]:
        """兜底扫描：有积压信号且超时未处理的会话（投递前崩溃补投）。"""
        rows = await memory_fetch(
            "SELECT session_id, app_id, user_id, pending_signals, pending_tokens "
            "FROM memory.memory_watermark "
            "WHERE (pending_signals > 0 OR pending_tokens > 0) "
            "AND updated_at < now() - make_interval(secs => $1::int) "
            "ORDER BY updated_at LIMIT $2",
            int(minutes) * 60, int(limit),
        )
        return [dict(r) for r in rows]

    async def advance_watermark(
        self, session_id: str, app_id: str, user_id: str, last_seq: int
    ) -> None:
        """推进水位并清零漏斗计数（仅在提取成功后调用）。

        pending_signals/pending_tokens 归零 + last_extract_at=now()；
        崩溃在推进前则计数保留，重试覆盖同一区间（幂等）。
        """
        await memory_execute(
            "INSERT INTO memory.memory_watermark "
            "(session_id, app_id, user_id, last_seq) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (session_id) DO UPDATE SET last_seq = EXCLUDED.last_seq, "
            "pending_signals = 0, pending_tokens = 0, last_extract_at = now()",
            session_id, str(app_id), str(user_id or ""), int(last_seq),
        )

    # === 内部 ===

    @staticmethod
    def _parse_payload(raw: Any) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (str, bytes)) and raw:
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}


# === 全局单例 ===

_global_memory_task_store: MemoryTaskStore | None = None


def get_memory_task_store() -> MemoryTaskStore:
    """全局任务存储单例（聊天在途高频登记 + 离线 worker 共用）。"""
    global _global_memory_task_store
    if _global_memory_task_store is None:
        _global_memory_task_store = MemoryTaskStore()
    return _global_memory_task_store
