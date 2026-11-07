"""
记忆离线任务 scheduler —— 常驻单消费者循环。

职责（设计方案 v2.1 §4/§6/§7/§11-P1）：
1. 从 memory_task 表领取到期任务，按类型分发执行：
   - warm_extract:       水位增量读对话（chat_message 表）→ 小模型提取候选
                         → 准入校验 → 分层落库（hot upsert / warm append）→ 推进水位
   - consolidate:        每日整理（warm 精确重复合并；hot 压缩为 P2-2）
   - decay_archive:      warm 30 天未命中软删 / 90 天归档（导出 + Qdrant 物理删除）
   - sync_check:         MySQL 真源 ↔ Qdrant 双向对账 + 抽检 + 进度回写
   - compliance_delete:  合规删除级联（MySQL → Qdrant，见 memory/compliance.py）
   - conflict_detect:    每日异步矛盾检测（P1-9：全量 hot 两两比对，保留新者）
2. 内嵌周期作业（P1-3/P1-4/P1-6/P1-7）：
   - 每 30s  向量同步 Worker：vector_synced_at IS NULL 队列批量补写
   - 每 60s  卡死回收巡检：running 超 10 分钟复位 pending；顺带刷新 dead 指标
   - 每 60s  兜底扫描：漏斗有积压信号且超时的会话补投 warm_extract
   - 每 5min 命中刷回：Redis mem:hit:* Hash 批量回写 MySQL（P1-7）
3. 任务执行带心跳（touch_running），防长任务被卡死回收误杀
4. 失败退避重试（1m/5m/30m，超限置 dead），任务语义 at-least-once
5. LLM 资源管控：独立小模型 + 信号量并发上限 + 在线让位（有活跃会话先让 1 秒）

生命周期由 services/agent_adapter_service.py 管理：
- startup: task_store.recover_running()（崩溃恢复）→ ensure_warm_collection → worker.start()
- shutdown: worker.stop(grace) —— 宽限等待 → 取消 → running 复位 pending
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from datetime import datetime

from shared import log

from codegenx.ai_service.utils.config import config, split_model_chain
from codegenx.ai_service.llm.async_client import get_llm
from codegenx.ai_service.llm.errors import classify_llm_error
from codegenx.ai_service.llm.resilience import get_breaker, resilient_invoke
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.memory.prompts import (
    MEMORY_CONFLICT_SYSTEM_PROMPT,
    MEMORY_EXTRACT_SYSTEM_PROMPT,
    format_constraints_for_conflict,
    format_conversation_for_extract,
    parse_conflict_pairs,
    parse_extracted_memories,
)
from codegenx.ai_service.memory.writer import write_memories
from codegenx.ai_service.schedule.memory_task_store import (
    MemoryTaskStore,
    get_memory_task_store,
    TASK_WARM_EXTRACT,
    TASK_CONSOLIDATE,
    TASK_DECAY_ARCHIVE,
    TASK_SYNC_CHECK,
    TASK_COMPLIANCE_DELETE,
    TASK_CONFLICT_DETECT,
)

# 单次提取消费的消息量上限（防止单任务过大拖垮小模型）
_MAX_MESSAGES_PER_EXTRACT = 80
_MESSAGE_TEXT_LIMIT = 600

# 内嵌周期作业间隔（秒）
_VECTOR_SYNC_INTERVAL = 30.0     # P1-4 向量同步 Worker
_PATROL_INTERVAL = 60.0          # P1-3 卡死回收 + 兜底扫描 + dead 指标
_HIT_FLUSH_INTERVAL = 300.0      # P1-7 命中刷回


class MemoryScheduler(BaseComponent):
    """记忆离线任务调度器（单消费者 + 内嵌周期作业），进程级组件。

    用法（组件化后由生命周期钩子驱动）:
        await scheduler.startup()         # async_before_start：崩溃恢复 + 启动循环
        await scheduler.shutdown(grace)   # async_before_stop：宽限等待 → 取消 → 复位
    """

    name = ComponentType.MEMORY_SCHEDULER

    def __init__(
        self,
        system_app=None,
        task_store: MemoryTaskStore | None = None,
        online_busy_fn=None,
        poll_interval: float = 15.0,
        batch_size: int = 5,
    ) -> None:
        BaseComponent.__init__(self, system_app)
        self.tasks = task_store or get_memory_task_store()
        # 在线忙检测（可选）：True 表示有活跃会话，LLM 调用前先让位
        self._online_busy = online_busy_fn
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        # 离线 LLM 并发上限：避免挤占在线对话资源
        self._sem = asyncio.Semaphore(config.memory.store.extract_max_concurrency or 2)
        # 初始化即解析提炼模型链（memory.store.model_name 逗号分隔，最高优先级，空则默认模型）
        self._model_chain: list[str] = split_model_chain(
            config.memory.store.model_name, config.get_default_model(),
        )
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        # 周期作业上次执行时刻
        self._last_vector_sync = 0.0
        self._last_patrol = 0.0
        self._last_hit_flush = 0.0

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    # === 组件生命周期钩子（调度器自身的启动/停止逻辑在此，容器只负责广播） ===

    async def async_before_start(self) -> None:
        """启动调度器；MySQL 暂不可用等失败只降级记忆离线功能，不阻断主服务。"""
        # 预热提炼模型的客户端与熔断器（构造即注册，不发起网络请求；失败不阻断启动）
        for model in self._model_chain:
            with contextlib.suppress(Exception):
                get_llm(model)
                get_breaker(model)
                log.info("[memory_scheduler] 记忆提炼模型客户端已预热: {}", model)
        # 预热 embedding 客户端：构造即注册熔断器 gauge（健康矩阵/告警启动即可见，
        # 不必等首次向量调用；语义层检索共用该进程内单例），不发网络请求
        with contextlib.suppress(Exception):
            from codegenx.ai_service.llm.embedding import get_embedding_client
            get_embedding_client()
            log.info("[memory_scheduler] embedding 客户端已预热")
        with contextlib.suppress(Exception):
            await self.startup()

    async def async_before_stop(self) -> None:
        """宽限 10s 停止（running 任务复位 pending，下次启动续跑）。"""
        with contextlib.suppress(Exception):
            await self.shutdown(grace=10.0)

    # === 生命周期 ===

    async def startup(self) -> None:
        """崩溃恢复 + 启动消费循环（幂等，必须在事件循环内调用）。"""
        try:
            await self.tasks.recover_running()
        except Exception:  # noqa: BLE001 — MySQL 暂不可用时不阻断主服务启动
            log.error("memory_task 崩溃恢复失败（MySQL 不可用？）\n{}", traceback.format_exc())
        if self._loop_task is None or self._loop_task.done():
            self._stop.clear()
            self._loop_task = asyncio.create_task(self._run_loop())
            log.info("MemoryScheduler 已启动（poll={}s）", self._poll_interval)

    async def shutdown(self, grace: float = 10.0) -> None:
        """停止：宽限等待在跑任务 → 超时取消 → running 复位 pending（下次续跑）。"""
        self._stop.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._loop_task), timeout=grace)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._loop_task
            self._loop_task = None
        try:
            recovered = await self.tasks.recover_running()
            log.info("MemoryScheduler 已停止（复位 running 任务 {} 个）", recovered)
        except Exception:  # noqa: BLE001
            log.warning("MemoryScheduler 停止时复位 running 任务失败")

    # === 消费循环 ===

    async def _run_loop(self) -> None:
        """单消费者主循环：周期作业 → 每日调度检查 → 领取 → 逐个执行 → 空闲轮询。"""
        last_schedule_check = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()

                # ── 内嵌周期作业（P1-3/P1-4/P1-7，各自容错不退出循环）────────────
                if now - self._last_vector_sync >= _VECTOR_SYNC_INTERVAL:
                    self._last_vector_sync = now
                    await self._run_vector_sync()
                if now - self._last_patrol >= _PATROL_INTERVAL:
                    self._last_patrol = now
                    await self._run_patrol()
                if now - self._last_hit_flush >= _HIT_FLUSH_INTERVAL:
                    self._last_hit_flush = now
                    await self._run_hit_flush()

                # 每分钟最多检查一次日调度（廉价查询）
                if now - last_schedule_check >= 60:
                    last_schedule_check = now
                    await self._schedule_daily_tasks()

                batch = await self.tasks.claim_due(limit=self._batch_size)
                if not batch:
                    # Python 3.12 起 asyncio.wait 只收 Task/Future，裸协程改用 wait_for
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
                    continue
                for task in batch:
                    if self._stop.is_set():
                        break  # 未跑任务仍为 running，shutdown() 统一复位
                    await self._process(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单轮异常不退出循环
                log.error("MemoryScheduler 循环异常\n{}", traceback.format_exc())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)

    # === 周期作业 ===

    async def _run_vector_sync(self) -> None:
        """P1-4 向量同步 Worker 单轮（批量 embedding + 批量 upsert）。"""
        try:
            from codegenx.ai_service.memory.sync import run_forward_sync_batch
            pushed = await run_forward_sync_batch()
            if pushed:
                log.debug("[vector_sync] 推送 {} 个点位", pushed)
        except Exception as exc:  # noqa: BLE001 — 失败留队列自动重试
            log.warning("[vector_sync] 批次失败（vector_synced_at 保持 NULL，下轮重试）: {}", exc)

    async def _run_patrol(self) -> None:
        """P1-3 巡检：卡死回收 + dead 指标 + 漏斗兜底扫描。"""
        try:
            reclaimed = await self.tasks.reclaim_stuck(timeout_sec=600)
            metrics.inc_stuck_reclaimed(reclaimed)
            if reclaimed:
                log.warning("[patrol] 卡死回收：{} 个 running 任务复位 pending", reclaimed)
        except Exception as exc:  # noqa: BLE001
            log.debug("[patrol] 卡死回收失败: {}", exc)
        try:
            stats = await self.tasks.stats()
            metrics.set_task_dead(int(stats.get("dead", 0)))
        except Exception:  # noqa: BLE001 — 指标失败无关业务
            pass
        # P1-2 兜底扫描：投递前崩溃的会话补投（§4.1）
        try:
            stale_minutes = config.memory.trigger.stale_scan_minutes or 15
            stale = await self.tasks.scan_stale_pending(minutes=stale_minutes, limit=20)
            for row in stale:
                enqueued = await self.tasks.enqueue(
                    TASK_WARM_EXTRACT,
                    app_id=str(row.get("app_id") or ""),
                    session_id=str(row.get("session_id") or ""),
                    user_id=str(row.get("user_id") or ""),
                    dedup=True,
                )
                if enqueued is not None:
                    metrics.inc_extract_triggered("stale_scan")
                    log.info(
                        "[patrol] 兜底补投 warm_extract: 会话 {}（积压 signals={} tokens={}）",
                        row.get("session_id"), row.get("pending_signals"), row.get("pending_tokens"),
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug("[patrol] 兜底扫描失败: {}", exc)

    async def _run_hit_flush(self) -> None:
        """P1-7 命中刷回：Redis Hash 聚合 → 批量回写 MySQL → DEL。"""
        try:
            from codegenx.ai_service.memory.hit_buffer import flush_all
            flushed = await flush_all()
            if flushed:
                log.debug("[hit_flush] 刷回 {} 条命中", flushed)
        except Exception as exc:  # noqa: BLE001 — 缓冲保留，下轮重试
            log.debug("[hit_flush] 刷回失败（缓冲保留）: {}", exc)

    async def _schedule_daily_tasks(self) -> None:
        """每日定时登记：consolidate / decay_archive / sync_check / conflict_detect。"""
        hour = datetime.now().hour
        trigger_hour = config.memory.store.consolidate_hour or 3
        if hour >= trigger_hour:
            if not await self.tasks.consolidate_scheduled_today():
                await self.tasks.enqueue(TASK_CONSOLIDATE, app_id="*")
                log.info("已登记今日跨会话整理任务（consolidate）")
            if not await self.tasks.decay_scheduled_today():
                await self.tasks.enqueue(TASK_DECAY_ARCHIVE, app_id="*")
                log.info("已登记今日衰减归档任务（decay_archive）")
            if not await self.tasks.sync_scheduled_today():
                await self.tasks.enqueue(TASK_SYNC_CHECK, app_id="*")
                log.info("已登记今日双数据源对账任务（sync_check）")
            # P1-9 矛盾检测：按租户投递（幂等键 app:user:日期，天然防重）
            if not await self.tasks.conflict_scheduled_today():
                enqueued = await self._enqueue_conflict_detect_all()
                if enqueued:
                    log.info("已登记今日矛盾检测任务（conflict_detect）× {} 租户", enqueued)

    async def _enqueue_conflict_detect_all(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        enqueued = 0
        try:
            from codegenx.ai_service.memory.memory_store import list_tenants
            tenants = await list_tenants()
        except Exception as exc:  # noqa: BLE001
            log.warning("[conflict_detect] 租户枚举失败: {}", exc)
            return 0
        for app_id, user_id in tenants:
            try:
                task_id = await self.tasks.enqueue(
                    TASK_CONFLICT_DETECT,
                    app_id=app_id,
                    user_id=user_id,
                    idempotency_key=f"{app_id}:{user_id}:{today}",
                )
                if task_id is not None:
                    enqueued += 1
            except Exception:  # noqa: BLE001 — 同键已投过（uk_idempotency 冲突）直接跳过
                continue
        return enqueued

    # === 任务执行 ===

    async def _process(self, task: dict) -> None:
        """执行单个任务：带心跳防误回收；失败走 mark_failed 退避重试。"""
        task_id = task["id"]
        task_type = task["task_type"]
        app_id = str(task.get("app_id") or "")
        started = time.time()
        heartbeat = asyncio.create_task(self._heartbeat_loop(task_id))
        try:
            produced, result_payload = 0, None
            # memory 不落 trace：任务表（心跳/mark_done/mark_failed）+ 专属指标已可监控，
            # trace 只保留框架监测基线；后台 trace 通道仅留给 session_summary
            if task_type == TASK_WARM_EXTRACT:
                produced, result_payload = await self._do_warm_extract(task)
            elif task_type == TASK_CONSOLIDATE:
                await self._do_consolidate(task)
            elif task_type == TASK_DECAY_ARCHIVE:
                await self._do_decay_archive(task)
            elif task_type == TASK_SYNC_CHECK:
                produced = await self._do_sync_check(task)
            elif task_type == TASK_COMPLIANCE_DELETE:
                await self._do_compliance_delete(task)
            elif task_type == TASK_CONFLICT_DETECT:
                produced = await self._do_conflict_detect(task)
            else:
                raise ValueError(f"未知任务类型: {task_type}")
            await self.tasks.mark_done(
                task_id, produced_count=int(produced or 0), result_payload=result_payload
            )
            if task_type == TASK_WARM_EXTRACT:
                metrics.inc_extract(app_id, "ok")
                metrics.observe_extract_latency(app_id, time.time() - started)
            log.debug("任务 #{} {} 完成，耗时 {:.1f}s", task_id, task_type, time.time() - started)
        except Exception as exc:  # noqa: BLE001
            # 异常闸（LLM调用设计方案.md §5.4）：按 LLMErrorClass 记因入库与日志，
            # 便于区分模型故障（retryable/超长）与本地 bug（logic）
            err_class = classify_llm_error(exc)
            status = await self.tasks.mark_failed(
                task_id, f"[{err_class.value}] {type(exc).__name__}: {exc}"
            )
            if task_type == TASK_WARM_EXTRACT:
                metrics.inc_extract(app_id, "dead" if status == "dead" else "failed")
            log.warning(
                "任务 #{} {} 失败（第 {} 次，{}）: {} → {}",
                task_id, task_type, task.get("retry_count", 0) + 1, err_class.value, exc, status,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat_loop(self, task_id: int) -> None:
        """§7.4 心跳：每 30s touch updated_at，防长任务被卡死回收巡检复位。"""
        while True:
            await asyncio.sleep(30.0)
            try:
                await self.tasks.touch_running(task_id)
            except Exception:  # noqa: BLE001 — 心跳失败不中断任务
                pass

    # === 任务实现 ===

    async def _do_warm_extract(self, task: dict) -> tuple[int, dict | None]:
        """增量提取：水位 → 完整轮次 → 小模型提取 → 准入校验分层落库 → 推进水位。

        只消费「以 assistant 收尾的完整轮次」：assistant 消息在 turn 结束后才入库，
        半截对话留在水位之后，等下个任务与后续轮次合并提取。
        会话锁（P1-6）：同会话并发提炼互斥；抢不到锁无过错重排（不计失败）。
        返回 (落库记忆条数, 任务审计 payload)。
        """
        app_id = str(task.get("app_id") or "")
        session_id = str(task.get("session_id") or "")
        user_id = str(task.get("user_id") or "")
        # 偏差①（P4 收尾）：任务携带会话归属智能体，供写门控与溯源
        agent_name = str(((task.get("payload") or {}).get("agent_name")) or "")
        if not app_id or not session_id:
            log.warning("[warm_extract] 任务缺少 app_id/session_id，跳过: #{}", task["id"])
            return 0, None

        # P1-6 会话锁：抢不到说明同会话在途，60s 后重试（reschedule 不计失败）
        from codegenx.ai_service.memory import locks
        token = await locks.acquire(session_id)
        if token is None:
            await self.tasks.reschedule(task["id"], delay_sec=60.0)
            return 0, None
        try:
            return await self._warm_extract_locked(task, app_id, session_id, user_id, agent_name)
        finally:
            await locks.release(session_id, token)

    def _memory_policy_for(self, agent_name: str):
        """会话归属智能体的 MemoryPolicy（偏差①写门控）；无归属/未配置 → None=不设限。"""
        if not agent_name:
            return None
        try:
            from codegenx.ai_service.system_app import get_app

            app = get_app()
            if app.agents is None:
                return None
            spec = app.agents.get(agent_name)
            return spec.memory if spec is not None else None
        except Exception:  # noqa: BLE001 — 策略解析失败不设限（默认行为），不阻断提炼
            return None

    async def _warm_extract_locked(
        self, task: dict, app_id: str, session_id: str, user_id: str,
        agent_name: str = "",
    ) -> tuple[int, dict | None]:
        # 1. 水位增量读取（chat_message 表为源，按 seq 递增）
        from codegenx.ai_service.chat_message import get_chat_message_store
        from_seq = await self.tasks.get_watermark(session_id)
        records = await get_chat_message_store().read_since(session_id, after_seq=from_seq)
        if not records:
            # BUG-2 修复：no-op 也要清账，否则 pending 永不清零 → patrol 每分钟重复补投
            await self.tasks.clear_pending(session_id)
            return 0, None

        # 2. 截到「以 assistant 收尾」的完整前缀；超过单次上限的留待下次
        consumed: list[tuple[int, dict]] = []
        for i, rec in enumerate(records):
            if rec[1].get("role") == "assistant":
                consumed = records[: i + 1]
        if not consumed:
            # 末尾没有完整轮次：清账退场；后续轮次完成时漏斗会重新累加并投递
            await self.tasks.clear_pending(session_id)
            return 0, None

        # 偏差①（P4 收尾）写门控：纯执行类智能体（spec.memory.write_enabled=False）
        # 不产生记忆——跳过提取与落库，水位直接排干积压防重复补投
        policy = self._memory_policy_for(agent_name)
        if policy is not None and not policy.write_enabled:
            await self.tasks.advance_watermark(session_id, app_id, user_id, int(records[-1][0]))
            log.debug("[warm_extract] 会话 {} 写门控关闭（agent={}），跳过提炼", session_id, agent_name)
            return 0, {"skipped": "write_disabled", "agent_name": agent_name}

        if len(consumed) > _MAX_MESSAGES_PER_EXTRACT:
            consumed = consumed[-_MAX_MESSAGES_PER_EXTRACT:]
            # 截断后必须仍以 assistant 收尾，否则本轮只读不推进
            while consumed and consumed[-1][1].get("role") != "assistant":
                consumed.pop()
            if not consumed:
                await self.tasks.clear_pending(session_id)
                return 0, None

        # 3. 小模型提取候选记忆
        started = time.time()
        turns_text = _render_messages([m for _, m in consumed])
        raw = await self._invoke_llm(
            messages=[
                {"role": "system", "content": MEMORY_EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": format_conversation_for_extract(turns_text)},
            ],
            max_tokens=1024,
        )
        candidates = parse_extracted_memories(raw)
        # 校验闸（§5.4）：模型有输出但解析不出候选 → 记因后按合法 no-op 推进水位
        #（「没提炼出记忆」是正常业务结果不重试；此日志用于发现提示词/模型回归）
        if raw and raw.strip() and not candidates:
            log.warning(
                "[warm_extract] 会话 {} 提炼输出无法解析为候选记忆（{} 字符），按 no-op 推进水位",
                session_id, len(raw),
            )

        # 4. 准入校验 + 分层落库（MySQL 真源先行，warm 向量层批量跟进）
        #    溯源（P2-7 ②）：消费区间内全部消息 uid 落 agent_memory.source_msg_ids
        #    偏差①：write_types 过滤允许提炼的类型；source_agent 落库溯源
        if candidates and policy is not None and policy.write_types:
            allowed = {str(t).strip() for t in policy.write_types if str(t).strip()}
            candidates = [c for c in candidates if str(c.get("memory_type") or "") in allowed]
        written_ids: list[str] = []
        if candidates:
            source_uids = [
                str(m.get("message_uid")) for _, m in consumed if m.get("message_uid")
            ]
            written_ids = await write_memories(
                user_id, app_id, session_id, candidates,
                source_msg_ids=source_uids or None, task_id=int(task["id"]),
                source_agent=agent_name or None,
            )
            if written_ids:
                log.info("[warm_extract] 会话 {} 写入 {} 条记忆", session_id, len(written_ids))

        # 5. 推进水位到消费到的最后一条 seq；LLM 失败已抛异常走重试，水位不动
        consumed_seq = consumed[-1][0]
        await self.tasks.advance_watermark(session_id, app_id, user_id, consumed_seq)

        # 任务审计（§4.2 ⑥ payload 回填：不含对话内容，只含元数据）
        payload = {
            "model": self._model_chain[0],
            "cost_ms": int((time.time() - started) * 1000),
            "from_seq": int(from_seq),
            "consumed_seq": consumed_seq,
            "source_msg_count": sum(1 for _, m in consumed if m.get("message_uid")),
            "memory_ids": written_ids,
            "agent_name": agent_name or None,
        }
        return len(written_ids), payload

    async def _do_consolidate(self, task: dict) -> None:
        """每日跨会话整理：warm 近似去重合并（纯规则）+ hot 层压缩（P2-2，耗 LLM）。

        hot 压缩仅在 active 条数达阈值（默认 hot_max 的 80%）时按组触发，
        见 memory/hot_compact.py；凌晨 idle 时段运行，LLM 与 conflict_detect
        共用同一 invoke。
        """
        from codegenx.ai_service.memory.lifecycle import consolidate_all_apps
        merged = await consolidate_all_apps()
        metrics.inc_consolidate_merged(int(merged or 0))
        try:
            from codegenx.ai_service.memory.hot_compact import compress_hot_all_apps
            stats = await compress_hot_all_apps(self._invoke_llm)
            if stats.get("groups_compressed"):
                log.info(
                    "[consolidate] hot 压缩: {} 租户 / {} 组 / 合并 {} 条",
                    stats.get("tenants_compressed"), stats.get("groups_compressed"),
                    stats.get("entries_merged"),
                )
        except Exception as exc:  # noqa: BLE001 — 压缩失败不影响 warm 合并结果
            log.error("[consolidate] hot 压缩异常: {}", exc)

    async def _do_decay_archive(self, task: dict) -> None:
        """衰减归档：30 天未访问软删除 + 90 天归档（jsonl→zip + Qdrant 删除）。"""
        from codegenx.ai_service.memory.lifecycle import decay_and_archive_all_apps
        soft_deleted, archived = await decay_and_archive_all_apps()
        metrics.inc_archived("soft_delete", int(soft_deleted or 0))
        metrics.inc_archived("archive", int(archived or 0))

    async def _do_sync_check(self, task: dict) -> int:
        """双数据源对账：MySQL 真源 ↔ Qdrant 双向（补写缺失 + 清理幽灵）+ 抽检。"""
        from codegenx.ai_service.memory.sync import reconcile_all_apps
        return await reconcile_all_apps(self._invoke_llm)

    async def _do_compliance_delete(self, task: dict) -> None:
        """合规删除级联（P0-10）：payload 携带 scope/target/hard/request_id。"""
        from codegenx.ai_service.memory.compliance import run_compliance_delete

        payload = task.get("payload") or {}
        app_id = str(task.get("app_id") or "")
        user_id = str(task.get("user_id") or "")
        scope = str(payload.get("scope") or "all")
        target = str(payload.get("target") or "")
        hard = bool(payload.get("hard", True))
        deleted = await run_compliance_delete(
            app_id, user_id, scope=scope, target=target, hard=hard,
            request_id=str(payload.get("request_id") or ""),
        )
        log.info("[compliance_delete] 任务 #{} 完成: scope={} deleted={}", task["id"], scope, deleted)

    async def _do_conflict_detect(self, task: dict) -> int:
        """P1-9 每日矛盾检测（§4.6）：全量 active hot 两两 LLM 比对。

        命中冲突 → 保留 created_at 更新的一条，旧的 markInactive(superseded)，
        审计日志不含内容。检测结果不影响写入（异步兜底定位）。
        """
        app_id = str(task.get("app_id") or "")
        user_id = str(task.get("user_id") or "")
        from codegenx.ai_service.memory.hot_store import load_hot_entries
        entries = await load_hot_entries(app_id, user_id)
        if len(entries) < 2:
            return 0

        numbered = [e.inject_text()[:200] for e in entries]
        raw = await self._invoke_llm(
            messages=[
                {"role": "system", "content": MEMORY_CONFLICT_SYSTEM_PROMPT},
                {"role": "user", "content": format_constraints_for_conflict(numbered)},
            ],
            max_tokens=512,
        )
        pairs = parse_conflict_pairs(raw)
        if not pairs:
            return 0

        from codegenx.ai_service.memory.models import STATUS_SUPERSEDED
        from codegenx.ai_service.memory.memory_store import mark_inactive

        resolved = 0
        for a_str, b_str in pairs:
            try:
                ia, ib = int(a_str) - 1, int(b_str) - 1
            except ValueError:
                continue
            if not (0 <= ia < len(entries) and 0 <= ib < len(entries)) or ia == ib:
                continue
            newer, older = entries[ia], entries[ib]
            if older.created_at > newer.created_at:  # ISO 字面量字典序即时间序
                newer, older = older, newer
            changed = await mark_inactive(
                app_id, user_id, [older.memory_id],
                STATUS_SUPERSEDED, superseded_by=newer.memory_id,
            )
            if changed:
                resolved += 1
                # 审计（§10.2：不含 summary/content）
                metrics.log_event(
                    "conflict_resolved",
                    app_id=app_id, user_id=user_id,
                    removed_memory_id=older.memory_id, kept_memory_id=newer.memory_id,
                    task_id=task["id"],
                )
        if resolved:
            log.warning(
                "[conflict_detect] {}/{} 解决 {} 对冲突（保留新者，旧者置 superseded）",
                app_id, user_id, resolved,
            )
        return resolved

    # === LLM 调用（小模型 + 并发管控）===

    async def _invoke_llm(self, messages: list[dict], max_tokens: int = 1024) -> str:
        """受控 LLM 调用：信号量限并发 + 在线让位（有活跃会话先等 1 秒）。

        走韧性层（提炼模型链 + 熔断/重试/降级）；模型在初始化时已从
        memory.store.model_name 解析（空则默认模型）。
        """
        async with self._sem:
            if self._online_busy is not None and self._online_busy():
                # 在线对话优先：短暂让位后再调用（只延迟一次，不无限等待）
                await asyncio.sleep(1.0)
            llm_response = await resilient_invoke(
                messages,
                chain=self._model_chain,
                label="memory",
                max_tokens=max_tokens,
                temperature=0.0,
            )
            return llm_response.get("content","")


# === 全局单例 ===

def get_memory_scheduler() -> MemoryScheduler:
    """全局 scheduler 组件（经容器查表；worker 循环与访问缓冲都要求进程内唯一）。"""
    from codegenx.ai_service.system_app import get_app

    return MemoryScheduler.get_instance(get_app())


def initialize_memory_scheduler(system_app) -> MemoryScheduler:
    """注册记忆调度器组件（system_app.initialize_components 调用）。"""
    return system_app.register(MemoryScheduler)


# === 消息渲染 ===

def _render_messages(messages: list[dict]) -> str:
    """把消息列表渲染为提取 prompt 输入（压缩 tool 内容，控制总长）。"""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "; ".join(str(item.get("content", "")) for item in content)
        content = str(content or "").strip()
        if content:
            parts.append(f"[{role}]: {content[:_MESSAGE_TEXT_LIMIT]}")
        for tc in msg.get("tool_calls", []) or []:
            name = tc.get("name") or (tc.get("function") or {}).get("name", "?")
            parts.append(f"[tool_call]: {name}")
    return "\n".join(parts)
