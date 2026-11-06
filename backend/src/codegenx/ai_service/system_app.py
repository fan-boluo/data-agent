"""SystemApp —— 全局组件容器（组合根）。

启动时注册全部进程级组件并广播生命周期；项目任意处通过 get_app() 获取。
分层原则与判别法见 docs/SystemApp架构设计.md §2：

┌─────────────────────────────────────────────────────────┐
│ SystemApp（进程级，全局一份，启动装配/关闭回收）              │
│  组件 = 无状态服务 | 注册表 | 客户端池 | 后台任务            │
├─────────────────────────────────────────────────────────┤
│ SessionState（会话级，SessionPool 管理，随会话生灭）          │
├─────────────────────────────────────────────────────────┤
│ TurnState（请求级，已有 ActivateTurn）                      │
└─────────────────────────────────────────────────────────┘

框架（LifeCycle/BaseComponent/组件注册表）在 component.py；本模块是组合根：
- initialize_components() 决定注册清单与顺序（顺序即依赖），末尾冻结 hook 注册表；
- 类型化访问器（get_app().xxx）按 ComponentType 惰性查表，业务代码零感知组件化。

导入约束（防循环）：组件模块在模块级只允许 import 本模块的框架再导出
（SystemApp/BaseComponent/ComponentType）或 component.py 框架本体；本模块
**只在 initialize_components()/访问器内部延迟 import 组件实现类**。
"""
from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from shared import log

# 框架层再导出：组件模块统一 `from codegenx.ai_service.system_app import ...` 或
# 直接 `from codegenx.ai_service.component import ...`，两者等价。
from codegenx.ai_service.component import (  # noqa: F401
    BaseComponent,
    ComponentType,
    LifeCycle,
)
from codegenx.ai_service.component import SystemApp as ComponentSystemApp


class SystemApp(ComponentSystemApp):
    """进程级容器：持有全部组件并提供类型化访问器。

    组件生命周期钩子实现在各自模块内（BaseComponent 子类），本类只负责
    装配与查表转发；未注册即访问会抛 ValueError（fail fast，宁可显式失败）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._components_ready: bool = False
        self._started: bool = False

    # ── 类型化访问器：惰性 import + 按组件名查表（注册清单见 initialize_components）──

    @property
    def llm(self) -> "LLMFacade":
        from codegenx.ai_service.llm.facade import LLMFacade

        return self.get_component(ComponentType.LLM_FACADE, LLMFacade)

    @property
    def tool_registry(self) -> "ToolRegistry":
        from codegenx.ai_service.agent.tool_handler import ToolRegistry

        return self.get_component(ComponentType.TOOL_REGISTRY, ToolRegistry)

    @property
    def tools_executor(self) -> "ToolExecutor":
        from codegenx.ai_service.agent.tool_executor import ToolExecutor

        return self.get_component(ComponentType.Tool_EXECUTOR, ToolExecutor)

    @property
    def skill_manager(self) -> "SkillManager":
        from codegenx.ai_service.skill.skill_loader import SkillManager

        return self.get_component(ComponentType.SKILL_MANAGER, SkillManager)

    @property
    def agents(self) -> "AgentRegistry":
        from codegenx.ai_service.agent.agent_registry import AgentRegistry

        return self.get_component(ComponentType.AGENT_REGISTRY, AgentRegistry)

    @property
    def session_pool(self) -> "SessionPool":
        from codegenx.ai_service.session.session_pool import SessionPool

        return self.get_component(ComponentType.SESSION_POOL, SessionPool)

    @property
    def session_io(self) -> "SessionPersistence":
        from codegenx.ai_service.chat_message.persist import SessionPersistence

        return self.get_component(ComponentType.SESSION_PERSISTENCE, SessionPersistence)

    @property
    def tasks(self) -> "TaskBoardService":
        from codegenx.ai_service.task.task_manager import TaskBoardService

        return self.get_component(ComponentType.TASK_BOARD, TaskBoardService)

    @property
    def memory(self) -> "MemoryFacade":
        from codegenx.ai_service.memory.memory_manager import MemoryFacade

        return self.get_component(ComponentType.MEMORY_MANAGER, MemoryFacade)

    @property
    def summary(self) -> "SessionSummaryService":
        from codegenx.ai_service.compact.session_summary import SessionSummaryService

        return self.get_component(ComponentType.SESSION_SUMMARY, SessionSummaryService)

    @property
    def compaction(self) -> "CompactionService":
        from codegenx.ai_service.compact.compact import CompactionService

        return self.get_component(ComponentType.COMPACTION, CompactionService)

    @property
    def chat_messages(self) -> "ChatMessageStore":
        from codegenx.ai_service.chat_message.store import ChatMessageStore

        return self.get_component(ComponentType.CHAT_MESSAGE_STORE, ChatMessageStore)

    @property
    def message_bus(self) -> "MessageBus":
        from codegenx.ai_service.bus.queue import MessageBus

        return self.get_component(ComponentType.MESSAGE_BUS, MessageBus)

    @property
    def monitor_maintenance(self) -> "MonitorMaintenanceService":
        from codegenx.ai_service.trace.maintenance import MonitorMaintenanceService

        return self.get_component(ComponentType.TRACE_MAINTENANCE, MonitorMaintenanceService)

    @property
    def memory_scheduler(self) -> "MemoryScheduler":
        from codegenx.ai_service.schedule.memory import MemoryScheduler

        return self.get_component(ComponentType.MEMORY_SCHEDULER, MemoryScheduler)

    @property
    def chat_service(self) -> "ChatService":
        from codegenx.ai_service.services.chat_service import ChatService

        return self.get_component(ComponentType.CHAT_SERVICE, ChatService)


# ── 进程级单容器访问 ─────────────────────────────────────────────────────────

_app: SystemApp | None = None


def get_app() -> SystemApp:
    """进程内任意处获取容器。未初始化时抛错（宁可 fail fast 也不静默兜底）。"""
    if _app is None:
        raise RuntimeError(
            "SystemApp 未初始化：请在应用启动入口调用 init_app()/start_app()"
        )
    return _app


def initialize_components(app: SystemApp) -> None:
    """注册全部组件并冻结 hook 注册表（幂等；顺序即依赖，勿随意调整）。

    每个组件模块提供 initialize_xxx(system_app) 完成构造+注册（DB-GPT 风格），
    构造知识留在组件模块内。注册顺序 = 启动广播顺序：
      llm → tools → tool_executor → skills → agents → context → session_io → tasks
      → memory → summary → compaction → chat_messages
      → monitor_maintenance → memory_scheduler → session_pool → chat_service
    """
    if app._components_ready:
        return

    from codegenx.ai_service.llm.facade import initialize_llm
    initialize_llm(app)

    from codegenx.ai_service.agent.tool_handler import initialize_tools
    initialize_tools(app)

    from codegenx.ai_service.agent.tool_executor import initialize_tool_executor
    initialize_tool_executor(app)

    from codegenx.ai_service.skill.skill_loader import initialize_skill
    initialize_skill(app)

    from codegenx.ai_service.agent.agent_registry import initialize_agents
    initialize_agents(app)

    from codegenx.ai_service.chat_message.persist import initialize_session_io
    initialize_session_io(app)

    from codegenx.ai_service.task.task_manager import initialize_tasks
    initialize_tasks(app)

    from codegenx.ai_service.memory.memory_manager import initialize_memory
    initialize_memory(app)

    from codegenx.ai_service.compact.session_summary import initialize_summary
    initialize_summary(app)

    from codegenx.ai_service.compact.compact import initialize_compaction
    initialize_compaction(app)

    from codegenx.ai_service.chat_message.store import initialize_chat_messages
    initialize_chat_messages(app)

    from codegenx.ai_service.bus.queue import initialize_message_bus
    initialize_message_bus(app)

    from codegenx.ai_service.trace.maintenance import initialize_trace_maintenance
    initialize_trace_maintenance(app)

    from codegenx.ai_service.schedule.memory import initialize_memory_scheduler
    initialize_memory_scheduler(app)

    from codegenx.ai_service.session.session_pool import initialize_session_pool
    initialize_session_pool(app)

    from codegenx.ai_service.services.chat_service import initialize_chat_service
    initialize_chat_service(app)

    # 智能体实例化与规格注册：所有组件已就绪（尤其 message_bus / tool_registry / skill_manager），
    # 各 Agent 在构造时读取自身 AgentConfig 并注册 AgentSpec 到 AgentRegistry。
    from codegenx.ai_service.agent.agent_instances import initialize_agent_instances
    initialize_agent_instances(app)

    # hook 注册表冻结：@on 监听器随组件模块的 import 链（模块加载即注册）收集完毕，
    # 此处统一校验/拓扑排序/冻结（docs/Hook设计.md §6）。
    # 注意：新增监听器模块必须位于组件依赖图或应用 import 链上，否则收集不到。
    # 显式加载 SQL 分析链路监听器（不在组件依赖图内，需主动 import 触发 @on 注册）
    from codegenx.ai_service.hook import analysis_trace  # noqa: F401
    from codegenx.ai_service.hook import tool_semantics  # noqa: F401  工具语义落表监听器
    # trace 生命周期（turn/session 直调函数 + store 初始化），观测不挂 hook
    from codegenx.ai_service.trace import recorder  # noqa: F401
    from codegenx.ai_service.hook import hook_manager

    hook_manager.load_and_freeze()

    app._components_ready = True
    log.info("SystemApp 组件装配完成：{} 个组件", len(app.components))


def init_app(app: SystemApp | None = None) -> SystemApp:
    """同步装配（main.py lifespan 前段调用；测试可传入自制实例）：

    创建/安装全局容器 → 注册组件（含 hook 冻结）→ 广播 on_init（同步钩子）。
    异步启动（连接/后台任务/runtime）由 start_app() 在事件循环内完成。
    """
    global _app
    if _app is None:
        _app = app if app is not None else SystemApp()
    initialize_components(_app)
    _app.on_init()
    return _app


async def start_app(app: SystemApp | None = None) -> SystemApp:
    """异步启动（需事件循环，lifespan 中调用）：广播 async_before_start（主启动钩子）。
    组件级失败语义由各组件钩子内部决定；
    幂等：已启动的容器重复调用直接返回（防重复拉起后台任务）。"""
    app = app or get_app()
    if app._started:
        return app
    await app.async_before_start()
    # 启动收尾：全部组件预热完成后广播（组件在此汇总依赖全体就绪的观测，如熔断器快照）
    await app.async_after_start()
    app._started = True
    log.info("SystemApp startup completed")
    return app


async def stop_app(app: SystemApp | None = None) -> None:
    """逆序关闭（lifespan shutdown 调用）：广播 async_before_stop（注册逆序，异常隔离）
    → 基础设施连接池收尾（redis/qdrant/mysql 为 db/ 模块级单例，不属于任何业务组件，
    由容器统一兜底释放；自 main.py lifespan 收编，close 本身幂等）。"""
    app = app or get_app()
    if not app._started:
        # 未成功启动过的容器无需收尾（lifespan 也只在成功 startup 后才调用 shutdown）
        return
    await app.async_before_stop()
    with suppress(Exception):
        from db.redis.redis_client import redis_client

        await redis_client.aclose()
    with suppress(Exception):
        from db.qdrant.client import shutdown_qdrant_client

        await shutdown_qdrant_client()
    with suppress(Exception):
        from db.mysql.session import shutdown_mysql_engine

        await shutdown_mysql_engine()
    with suppress(Exception):
        from db.postgres.postgres_client import shutdown_postgres_engine

        await shutdown_postgres_engine()
    app._started = False
    log.info("SystemApp shutdown completed")


def reset_app() -> None:
    """仅测试用：卸载容器，配合 pytest fixture。"""
    global _app
    _app = None


def app_started() -> bool:
    """容器是否已完成启动（诊断/测试用）。"""
    return _app is not None and _app._started
