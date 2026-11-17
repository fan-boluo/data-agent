"""
记忆门面 —— 每轮组装 hot + warm 两层注入（P2 服务化，docs/SystemApp架构设计.md §4.3）。

原 MemoryManager（每会话一个实例、只持 3 个 id 字符串）改为全局无状态 MemoryFacade：
ids 进签名（user_id=/app_id=/session_id=），内部模块函数不动。

分层（设计方案 v2.1 §1.1/§5.5）：
  hot  hot_store.format_hot_prompt  核心约束，每轮全量注入（MySQL，≤hot_token_budget）
  warm retriever.search_warm        情景记忆，按 query 召回 + 应用层重排（≤warm_token_budget）
  冲突消解规则注入尾部（P0-12）：hot > warm > 用户历史，用户当前指令最高

写入不在这里：条件触发的提取走 schedule/ 离线任务，本类只读。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.memory.trigger import process_session_end
from codegenx.ai_service.system_app import SystemApp
from shared import log
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.memory import metrics
from codegenx.ai_service.memory.hot_store import format_hot_prompt
from codegenx.ai_service.memory.retriever import search_warm, format_warm_entries_prompt
from codegenx.ai_service.hook import HookContext, HookEvent, on

# 冲突消解规则（P0-12，§5.5）：跨层 hot 优先，不按时间推翻硬约束
MEMORY_USAGE_RULES = """\
# 记忆冲突处理规则
- 核心约束（hot）与历史经验（warm）冲突时，一律以核心约束为准
- 同一层级内出现冲突时，以时间更新的记忆为准
- 若发现记忆与用户当前明确指令冲突，以用户当前指令为准\
"""


class MemoryFacade(BaseComponent):
    """全局只读记忆门面（hot/warm 仍按 app+user 维度，session 仅用于埋点）。"""

    name = ComponentType.MEMORY_MANAGER

    def init_app(self, system_app: SystemApp) -> None:
        self.system_app = system_app

    async def async_before_start(self) -> None:
        """基础设施预热（迁自旧 SystemApp._startup_infra）：
        PostgreSQL/pgvector 暂不可用只降级记忆功能，不阻断主服务。"""
        with suppress(Exception):
            from db.postgres.postgres_client import get_memory_pool
            from codegenx.ai_service.memory.store_init import ensure_memory_schema

            await get_memory_pool()
            await ensure_memory_schema()
            log.info("记忆域 PostgreSQL（agent schema）已就绪")

    async def load(
        self,
        query: str = "",
        *,
        user_id: str = "",
        app_id: str = "",
        session_id: str = "",
        read_types: list[str] | None = None,
    ) -> str:
        """组装当前轮的记忆注入：hot（常驻）+ warm（按 query 召回）+ 冲突规则。

        Args:
            query: 用户本轮输入，空则跳过 warm 召回。
            read_types: P4 §10.5 记忆类型过滤（AgentSpec.memory.read_types）；
                None=全部；warm 层按 memory_type 过滤，hot 层为核心约束不过滤。
        """
        if not config.memory.search.enabled:
            return ""

        parts: list[str] = []
        search_cfg = config.memory.search
        hot_timeout = max(0.05, (search_cfg.hot_load_timeout_ms or 200) / 1000)
        warm_timeout = max(0.05, (search_cfg.warm_load_timeout_ms or 500) / 1000)

        # ── hot 层：核心约束，每轮注入（超时/异常均降级为空，§9 原则 1）──────────
        try:
            hot_prompt = await asyncio.wait_for(
                format_hot_prompt(app_id, user_id), timeout=hot_timeout
            )
        except asyncio.TimeoutError:
            metrics.inc_degrade("timeout")
            log.warning("hot 层加载超时(>{}ms)，本轮降级为无 hot 约束", int(hot_timeout * 1000))
            hot_prompt = ""
        except Exception as exc:  # noqa: BLE001 — 记忆加载不得阻塞对话
            metrics.inc_degrade("mysql")
            log.error("hot 层注入异常:{}", exc)
            hot_prompt = ""
        if hot_prompt:
            parts.append(hot_prompt)

        # ── warm 层：按 query 混合召回（超时/异常降级，§9 原则 1）────────────────
        warm_entries: list = []
        if (query or "").strip():
            try:
                warm_entries = await asyncio.wait_for(
                    search_warm(app_id, user_id, query), timeout=warm_timeout
                )
            except asyncio.TimeoutError:
                metrics.inc_degrade("timeout")
                log.warning("warm 召回超时(>{}ms)，本轮降级为无 warm 记忆", int(warm_timeout * 1000))
                warm_entries = []
            except Exception as exc:  # noqa: BLE001 — 记忆检索失败不阻断对话
                metrics.inc_degrade("mysql")
                log.error("warm 记忆检索异常:{}", exc)
                warm_entries = []
            if warm_entries and read_types is not None:
                # P4：按 AgentSpec.memory.read_types 过滤（类型体系见 memory/models.py）
                allowed = {str(t).strip() for t in read_types if str(t).strip()}
                warm_entries = [e for e in warm_entries if e.memory_type in allowed]
            if warm_entries:
                parts.append(format_warm_entries_prompt(warm_entries))

        # ── 冲突消解规则（有记忆注入才附带）────────────────────────────────────
        if parts:
            parts.append(MEMORY_USAGE_RULES)

        return "\n\n".join(parts)

    @on(HookEvent.SESSION_END, name="memory_session_end", priority=20)
    async def memory_session_end(ctx: HookContext) -> None:
        """会话结束事件是提炼触发条件之一（P1-2 §4.1，迁自 handlers.on_session_end 前半）。"""
        session = ctx.session
        try:
            await process_session_end(
                str(getattr(session, "app_id", "") or ""),
                str(getattr(session, "user_id", "") or ""),
                str(getattr(session, "session_id", "") or ""),
                agent_name=str(getattr(session, "agent_name", "") or ""),
            )
        except Exception as exc:
            log.debug("会话结束记忆触发失败（非致命）: {}", exc)



def initialize_memory(system_app) -> MemoryFacade:
    """注册记忆门面组件（system_app.initialize_components 调用）。"""
    return system_app.register(MemoryFacade)
