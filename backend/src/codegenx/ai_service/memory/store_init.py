"""
记忆域 PG 启动自检（幂等）。

建表脚本是 init/pg_memory_schema.sql（真源）；这里只做运行期低风险补齐：
  - 扩展与 schema 存在性（缺失时尝试创建，权限不足只告警）
  - chat_message 未来月份分区（分区不存在会导致写入失败，必须运行期兜底）
不在这里建表：DDL 变更统一走 init/pg_memory_schema.sql，避免两处维护。
"""
from __future__ import annotations

from db.postgres.postgres_client import memory_execute, get_memory_pool
from shared import log


async def ensure_memory_schema() -> None:
    """连接预热 + 扩展/schema 自检 + 分区补齐（失败只告警，不阻断启动）。"""
    pool = await get_memory_pool()
    async with pool.acquire() as conn:
        for ddl in (
            "CREATE EXTENSION IF NOT EXISTS vector",
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE SCHEMA IF NOT EXISTS memory",
            "CREATE SCHEMA IF NOT EXISTS message",
        ):
            try:
                await conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001 — 权限不足等只降级告警
                log.warning("记忆域初始化语句失败（可手工执行 init/pg_memory_schema.sql）: {} | {}", ddl, exc)

    # 补齐未来 3 个月分区（函数由建表脚本创建，缺失说明尚未初始化）
    try:
        created = await memory_execute("SELECT message.ensure_chat_message_partitions(3)")
        if created:
            log.info("chat_message 新增分区数: {}", created)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat_message 分区补齐失败（请确认已执行 init/pg_memory_schema.sql）: {}", exc)
