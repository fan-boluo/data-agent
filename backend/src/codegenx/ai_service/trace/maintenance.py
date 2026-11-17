"""trace 维护服务：telemetry 保留期清理（traces / trace_spans）。

周期调度已统一收口到 `schedule/retention.py`（RetentionScheduler），本模块只提供
可被调度器/管理端直接调用的清理实现，不再自持后台循环。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shared import log

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.system_app import SystemApp
from codegenx.ai_service.trace.schema.admin import MonitorCleanupSummary, MonitorCleanupTableResult
from db.postgres.postgres_client import get_postgres_pool


class MonitorMaintenanceService(BaseComponent):
    """telemetry 保留期清理服务（无后台任务，由 retention scheduler 或管理端 API 触发）。"""

    name = ComponentType.TRACE_MAINTENANCE

    def __init__(self, system_app: SystemApp | None = None) -> None:
        BaseComponent.__init__(self, system_app)

    def init_app(self, system_app: SystemApp) -> None:
        self.system_app = system_app

    async def cleanup_history(self, *, retention_days: int = 7, dry_run: bool = False) -> MonitorCleanupSummary:
        """按保留期清理 telemetry（先 spans 后 traces，同一 trace 随主行删除）。"""
        cutoff_at = datetime.now(UTC) - timedelta(days=max(retention_days, 1))
        table_results: list[MonitorCleanupTableResult] = []
        total_affected = 0
        overall_status = "success"

        pool = await get_postgres_pool()
        async with pool.acquire() as conn:
            # 1) 过期 traces 的 spans（子表）
            spans_deleted = traces_deleted = 0
            try:
                expired = await conn.fetchval(
                    "SELECT COUNT(*) FROM telemetry.traces WHERE started_at < $1", cutoff_at
                )
                if not dry_run and expired:
                    spans_deleted = await conn.execute(
                        "DELETE FROM telemetry.trace_spans WHERE trace_id IN "
                        "(SELECT trace_id FROM telemetry.traces WHERE started_at < $1)",
                        cutoff_at,
                    )
                    traces_deleted = await conn.execute(
                        "DELETE FROM telemetry.traces WHERE started_at < $1", cutoff_at
                    )
                total_affected += int(expired or 0)
                table_results.append(MonitorCleanupTableResult(
                    tableName="telemetry.traces",
                    status="success",
                    affectedRows=int(expired or 0),
                    cutoffAt=cutoff_at,
                ))
                if spans_deleted or traces_deleted:
                    log.info("telemetry cleanup: {} {}",
                             spans_deleted.split()[-1] if spans_deleted else 0,
                             traces_deleted.split()[-1] if traces_deleted else 0)
            except Exception as exc:
                overall_status = "partial"
                table_results.append(MonitorCleanupTableResult(
                    tableName="telemetry.traces", status="error",
                    affectedRows=0, cutoffAt=cutoff_at, errorMessage=str(exc),
                ))

            # 2) 孤儿 spans（主行已删而 spans 残留的兜底清理）
            try:
                orphan = await conn.fetchval(
                    "SELECT COUNT(*) FROM telemetry.trace_spans s "
                    "WHERE NOT EXISTS (SELECT 1 FROM telemetry.traces t WHERE t.trace_id = s.trace_id)"
                )
                if not dry_run and orphan:
                    await conn.execute(
                        "DELETE FROM telemetry.trace_spans s "
                        "WHERE NOT EXISTS (SELECT 1 FROM telemetry.traces t WHERE t.trace_id = s.trace_id)"
                    )
                total_affected += int(orphan or 0)
                table_results.append(MonitorCleanupTableResult(
                    tableName="telemetry.trace_spans (orphan)",
                    status="success",
                    affectedRows=int(orphan or 0),
                    cutoffAt=cutoff_at,
                ))
            except Exception as exc:
                overall_status = "partial"
                table_results.append(MonitorCleanupTableResult(
                    tableName="telemetry.trace_spans (orphan)", status="error",
                    affectedRows=0, cutoffAt=cutoff_at, errorMessage=str(exc),
                ))

        return MonitorCleanupSummary(
            retentionDays=max(retention_days, 1),
            dryRun=dry_run,
            status=overall_status,
            deletedRows=total_affected,
            executedAt=datetime.now(UTC),
            tableResults=table_results,
        )


def get_monitor_maintenance_service() -> MonitorMaintenanceService:
    """取 trace 维护组件（经全局容器查表；容器未初始化时 fail fast）。"""
    from codegenx.ai_service.system_app import get_app

    return MonitorMaintenanceService.get_instance(get_app())


def initialize_trace_maintenance(system_app: SystemApp) -> MonitorMaintenanceService:
    """注册 trace 维护组件（system_app.initialize_components 调用）。"""
    return system_app.register(MonitorMaintenanceService)
