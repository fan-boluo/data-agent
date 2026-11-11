"""
MysqlManager — MySQL 只读连接管理

职责：
- 管理连接池
- 执行只读查询
- SQL 注入防护（仅允许 SELECT/SHOW/DESCRIBE/EXPLAIN）
- 超时控制
- 大表自动采样

连接配置从应用配置获取。
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from shared import log

# 可选依赖
try:
    import aiomysql
    _HAS_MYSQL = True
except ImportError:
    aiomysql = None  # type: ignore
    _HAS_MYSQL = False

# 默认超时（秒）
DEFAULT_QUERY_TIMEOUT = 30

# 行数估算查询
_TABLE_ROWS_SQL = (
    "SELECT TABLE_ROWS FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)

# 大表采样阈值
LARGE_TABLE_ROWS = 1_000_000   # 100 万行 → 开始降级
VERY_LARGE_TABLE_ROWS = 5_000_000  # 500 万行 → 10%
HUGE_TABLE_ROWS = 10_000_000  # 1000 万行 → 1%


@dataclass
class MysqlConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "readonly"
    password: str = ""
    databases: list[str] | None = None  # 白名单，None = 不限制
    pool_size: int = 5
    connect_timeout: int = 10




def resolve_sample_ratio(est_rows: int) -> tuple[float, str]:
    """按估算行数解析统计采样比例：大表自动降采样，返回 (比例 0-1, 说明文本)。"""
    if est_rows <= 100_000:
        return 1.0, "全量统计"
    if est_rows <= 1_000_000:
        return 0.1, "10% 采样（大表）"
    if est_rows <= 10_000_000:
        return 0.01, "1% 采样（超大表）"
    return 0.001, "0.1% 采样（巨型表）"


class MysqlManager:
    """MySQL 只读连接管理。"""

    def __init__(self, config: MysqlConfig | None = None):
        self.config = config or MysqlConfig()
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self):
        if self._pool is not None:
            return
        async with self._lock:
            if self._pool is not None:
                return
            if not _HAS_MYSQL:
                raise RuntimeError("aiomysql 未安装，无法使用 MySQL 工具")
            self._pool = await aiomysql.create_pool(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                minsize=1,
                maxsize=self.config.pool_size,
                connect_timeout=self.config.connect_timeout,
                autocommit=True,
            )

    async def query(
        self,
        db_name: str,
        sql: str,
        params: tuple | None = None,
        timeout_seconds: int = DEFAULT_QUERY_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """执行只读查询，返回 list[dict]。
        自动检查 SQL 安全性、数据库白名单、超时。
        """
        # 安全检查
        from codegenx.ai_service.utils.dbutils import check_sql_safety
        check_sql_safety(sql)

        # 数据库白名单
        if self.config.databases and db_name not in self.config.databases:
            raise ValueError(f"数据库 {db_name} 不在白名单中: {self.config.databases}")

        await self._ensure_pool()

        try:
            async with self._pool.acquire() as conn:
                # 超时控制
                cursor = await conn.cursor(aiomysql.DictCursor)
                try:
                    await asyncio.wait_for(
                        cursor.execute(f"USE `{db_name}`"),
                        timeout=timeout_seconds,
                    )
                    if params:
                        await asyncio.wait_for(
                            cursor.execute(sql, params),
                            timeout=timeout_seconds,
                        )
                    else:
                        await asyncio.wait_for(
                            cursor.execute(sql),
                            timeout=timeout_seconds,
                        )
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
                except asyncio.TimeoutError:
                    log.warning("MySQL 查询超时 ({}s): {}", timeout_seconds, sql[:120])
                    raise
                finally:
                    await cursor.close()
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            log.error("MySQL 查询异常: {}", exc)
            raise

    async def get_table_rows_estimate(self, db_name: str, table_name: str) -> int:
        """从 information_schema 估算行数。"""
        rows = await self.query(db_name, _TABLE_ROWS_SQL, (db_name, table_name))
        if rows and rows[0].get("TABLE_ROWS"):
            return int(rows[0]["TABLE_ROWS"])
        return 0

    async def close(self):
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None


# ── 进程级单例（连接参数来自 .env，经 shared.settings 读取） ──────────
_manager: MysqlManager | None = None


def get_mysql_manager() -> MysqlManager:
    global _manager
    if _manager is None:
        from shared import settings
        _manager = MysqlManager(MysqlConfig(
            host=settings.userdb_mysql_host,
            port=settings.userdb_mysql_port,
            user=settings.userdb_mysql_user,
            password=settings.userdb_mysql_password
        ))
    return _manager
