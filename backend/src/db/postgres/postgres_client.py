"""
PostgreSQL 异步连接池（asyncpg）。

用途：语义层元数据真源（semantic schema）读写 + pgvector 检索。
业务数仓连接不经过本模块，由 semantic/catalog 的数据源注册单独管理。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote_plus

import asyncpg

from shared import log
from shared.config.config import get_settings

# 进程级连接池（懒加载：启动阶段不建连，避免配置缺失导致启动失败）
_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


def postgres_dsn() -> str:
    """应用 PG 元数据库 DSN（凭据全部来自 .env）。"""
    s = get_settings()
    return (
        "postgresql://"
        f"{quote_plus(s.postgres_user)}:{quote_plus(s.postgres_password)}"
        f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"
    )

def postgres_business(business_db:str) -> str:
    """业务库PG"""
    s = get_settings()
    return (
        "postgresql://"
        f"{quote_plus(s.postgres_user)}:{quote_plus(s.postgres_password)}"
        f"@{s.postgres_host}:{s.postgres_port}/{business_db}"
    )

async def get_postgres_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                s = get_settings()
                _pool = await asyncpg.create_pool(
                    dsn=postgres_dsn(),
                    min_size=max(1, s.postgres_pool_min),
                    max_size=max(s.postgres_pool_min, s.postgres_pool_max),
                    command_timeout=30,
                )
                log.info(
                    "PostgreSQL 连接池已创建: {}:{}/{}",
                    s.postgres_host, s.postgres_port, s.postgres_db,
                )
    return _pool


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_postgres_pool()
    return await pool.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    pool = await get_postgres_pool()
    return await pool.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    pool = await get_postgres_pool()
    return await pool.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    pool = await get_postgres_pool()
    return await pool.execute(sql, *args)


async def close_postgres_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("PostgreSQL 连接池已关闭")


class PostgresClient:
    """兼容既有占位类：按需取池，不持有连接。"""

    async def shutdown(self) -> None:
        await close_postgres_pool()


async def shutdown_postgres_engine() -> None:
    """语义层与记忆域两个池一并释放（各自幂等）。"""
    await close_postgres_pool()
    await close_memory_pool()


# ===========================================================================
# 记忆域连接池（agent.*：agent_memory / chat_message 等）
# 与语义层分池：聊天写入与提炼 Worker 是高频短事务，独立池避免互相饿死。
# ===========================================================================
_memory_pool: asyncpg.Pool | None = None
_memory_pool_lock = asyncio.Lock()


async def get_memory_pool() -> asyncpg.Pool:
    """记忆域连接池（懒加载）。"""
    global _memory_pool
    if _memory_pool is None:
        async with _memory_pool_lock:
            if _memory_pool is None:
                s = get_settings()
                _memory_pool = await asyncpg.create_pool(
                    dsn=postgres_dsn(),
                    min_size=max(1, s.postgres_memory_pool_min),
                    max_size=max(s.postgres_memory_pool_min, s.postgres_memory_pool_max),
                    command_timeout=30,
                )
                log.info(
                    "记忆域 PostgreSQL 连接池已创建: {}:{}/{}",
                    s.postgres_host, s.postgres_port, s.postgres_db,
                )
    return _memory_pool


async def memory_fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_memory_pool()
    return await pool.fetch(sql, *args)


async def memory_fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    pool = await get_memory_pool()
    return await pool.fetchrow(sql, *args)


async def memory_fetchval(sql: str, *args: Any) -> Any:
    pool = await get_memory_pool()
    return await pool.fetchval(sql, *args)


async def memory_execute(sql: str, *args: Any) -> str:
    pool = await get_memory_pool()
    return await pool.execute(sql, *args)


@asynccontextmanager
async def memory_transaction() -> AsyncIterator[asyncpg.Connection]:
    """记忆域事务：同一连接内多语句原子提交（如「写记忆 + 推进水位」）。

    用法：
        async with memory_transaction() as conn:
            await conn.execute(...)
    """
    pool = await get_memory_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


async def close_memory_pool() -> None:
    global _memory_pool
    if _memory_pool is not None:
        await _memory_pool.close()
        _memory_pool = None
        log.info("记忆域 PostgreSQL 连接池已关闭")
