"""数据保留期清理调度（统一清理入口）。

三条保留期相互独立，各管各的数据：
- chat_message：`session.chatMessageDays`，按时间删，但**受记忆提炼水位保护**
  （只删 seq <= agent.memory_watermark.last_seq 的行，防止未提炼的记忆源被清掉）；
- telemetry：`trace.retentionDays`，traces / trace_spans 按时间删；
- session 目录：`session.sessionDays`，**整个目录直接删除**（SUMMARY.md、
  summary_state.json、message_blobs 外置消息、*_persist.txt 等一并清掉，
  不再单独清理大输出文件）。

注意：`code/` 是用户自己的工作区，其中图表/报告（`agent_output/`）由用户自行管理，
本调度不清理，避免误删用户文件。

memory 的 decay/consolidate 有独立 scheduler（memory_scheduler），不在此列。
调度形态：每 interval_seconds 检查一次，按"每天一次"执行（启动后首个 tick 即跑）。
"""
from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shared import log
from shared.constants import DATA_ROOT_DIR

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.system_app import SystemApp
from codegenx.ai_service.utils.config import config


class RetentionScheduler(BaseComponent):
    """过期数据清理调度器（进程内单实例，后台任务生命周期由组件钩子管理）。"""

    name = ComponentType.RETENTION_SCHEDULER

    def __init__(self, system_app: SystemApp | None = None) -> None:
        BaseComponent.__init__(self, system_app)
        cfg = config.retention
        self.enabled = bool(cfg.enabled)
        self.interval_seconds = max(int(cfg.interval_seconds or 3600), 60)
        self.chat_message_days = int(config.session.chat_message_days)
        self.session_days = int(config.session.session_days)
        self.telemetry_days = int(config.trace.retention_days)
        self._task: asyncio.Task | None = None
        self._last_run_date = None

    def init_app(self, system_app: SystemApp) -> None:
        self.system_app = system_app

    async def async_before_start(self) -> None:
        if not self.enabled:
            log.info("RetentionScheduler 已关闭（config.retention.enabled=false）")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="retention-scheduler")
        log.info("RetentionScheduler started (interval={}s, 每日执行一次)", self.interval_seconds)

    async def async_before_stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            log.info("RetentionScheduler stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                today = datetime.now(UTC).date()
                if self._last_run_date == today:
                    continue
                await self.run_once()
                self._last_run_date = today
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("RetentionScheduler 执行失败")

    async def run_once(self) -> dict[str, int]:
        """执行一轮全部清理任务，返回各项处理数量（失败项为 0）。"""
        summary: dict[str, int] = {
            "chat_messages": await self._cleanup_chat_messages(),
            "telemetry": await self._cleanup_telemetry(),
        }
        # 文件类清理是同步 IO，放 asyncio 全局内置共享线程池,避免阻塞事件循环
        summary.update(await asyncio.to_thread(self._cleanup_files))

        log.info(
            "retention 清理完成 | chat_messages={} telemetry={} session_dirs={}",
            summary.get("chat_messages", 0), summary.get("telemetry", 0),
            summary.get("session_dirs", 0),
        )
        return summary

    # === chat_message（记忆水位保护） ===

    async def _cleanup_chat_messages(self) -> int:
        from codegenx.ai_service.chat_message import get_chat_message_store

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=max(self.chat_message_days, 1)
        )
        try:
            deleted = await get_chat_message_store().delete_extracted_before(cutoff)
        except Exception as exc:
            log.warning("清理过期对话失败: {}", exc)
            return 0
        if deleted:
            log.info("清理过期对话 | deleted={} | retention={}d", deleted, self.chat_message_days)
        return deleted

    # === telemetry（按时间，与 chat_message 分开管理） ===

    async def _cleanup_telemetry(self) -> int:
        from codegenx.ai_service.trace.maintenance import get_monitor_maintenance_service

        try:
            result = await get_monitor_maintenance_service().cleanup_history(
                retention_days=max(self.telemetry_days, 1), dry_run=False,
            )
            return int(result.deleted_rows or 0)
        except Exception as exc:
            log.warning("清理过期链路数据失败: {}", exc)
            return 0

    # === 文件（会话目录整体删除） ===

    def _cleanup_files(self) -> dict[str, int]:
        return {"session_dirs": self._cleanup_session_dirs()}

    def _cleanup_session_dirs(self) -> int:
        """直接删除超过保留期未更新的会话目录。

        会话目录内含 SUMMARY.md / summary_state.json / message_blobs /
        *_persist.txt 等，整体删除后无需再单独清理各类文件。
        """
        cutoff = time.time() - max(self.session_days, 1) * 86400
        removed = 0
        for root in _iter_app_subdirs("session"):
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    if _last_activity(child) >= cutoff:
                        continue
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
                except OSError:
                    continue
        if removed:
            log.info("清理过期会话目录 | removed={} | retention={}d", removed, self.session_days)
        return removed


def _last_activity(path: Path) -> float:
    """目录最近活动时间：目录自身与所有子孙文件 mtime 的最大值。"""
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    return newest


def _iter_app_subdirs(name: str):
    """遍历 .data/{user}/{app}/{name} 目录（不存在则跳过）。"""
    if not DATA_ROOT_DIR.is_dir():
        return
    for user_dir in DATA_ROOT_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for app_dir in user_dir.iterdir():
            target = app_dir / name
            if target.is_dir():
                yield target


def get_retention_scheduler() -> RetentionScheduler:
    """取清理调度组件（经全局容器查表；容器未初始化时 fail fast）。"""
    from codegenx.ai_service.system_app import get_app

    return RetentionScheduler.get_instance(get_app())


def initialize_retention_scheduler(system_app: SystemApp) -> RetentionScheduler:
    """注册清理调度组件（system_app.initialize_components 调用）。"""
    return system_app.register(RetentionScheduler)
