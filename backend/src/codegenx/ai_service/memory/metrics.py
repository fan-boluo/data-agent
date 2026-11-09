"""
记忆系统指标与结构化日志门面（P1-12，设计方案 v2.1 §10）。

原则：
- 埋点绝不抛异常、绝不阻断业务（prometheus_client 本身不抛，这里再兜一层）；
- 指标只保留「当前状态 Gauge（可告警）+ 质量趋势」；任务完成明细以
  memory_task / memory_sync_checkpoint 表为真源，描述性事件走 log_event；
- summary/content 一律不进日志（§10.2 硬要求），排错用 memory_id 回查 MySQL。
"""
from __future__ import annotations

from shared import log


def _m():
    from codegenx.ai_service.trace import metrics as m
    return m


def inc_extract(app_id: str, status: str) -> None:
    """warm_extract 任务结果计数（status: ok/failed/dead）——质量趋势用。"""
    try:
        _m().memory_extract_total.labels(app_id=app_id or "", status=status).inc()
    except Exception:  # noqa: BLE001 — 埋点失败静默
        pass


def set_task_queue(pending: int, running: int, dead: int) -> None:
    """任务队列水位（patrol 每 60s 刷新）：pending 积压/running 卡死/dead 累积失败。"""
    try:
        _m().memory_task_pending.set(max(0, int(pending)))
        _m().memory_task_running.set(max(0, int(running)))
        _m().memory_task_dead.set(max(0, int(dead)))
    except Exception:  # noqa: BLE001
        pass


def set_hot_ratio(token_ratio: float) -> None:
    """hot 层注入水位（最近一次采样的用户维度值）。"""
    try:
        _m().memory_hot_token_usage_ratio.set(max(0.0, float(token_ratio)))
    except Exception:  # noqa: BLE001
        pass


def inc_degrade(kind: str) -> None:
    """降级打点（§9 原则 2：降级必须可观测）。kind: postgres/redis/embedding/timeout。"""
    try:
        _m().memory_degrade_total.labels(kind=kind).inc()
    except Exception:  # noqa: BLE001
        pass


def log_event(event: str, **fields) -> None:
    """§10.2 结构化操作日志：仅元数据字段，禁止传 summary/content。"""
    try:
        rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        log.info("[memory:{}]{}", event, f" {rendered}" if rendered else "")
    except Exception:  # noqa: BLE001
        pass
