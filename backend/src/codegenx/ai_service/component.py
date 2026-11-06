"""组件框架：LifeCycle 生命周期钩子 + BaseComponent + SystemApp 组件注册表。

分层原则与判别法见 docs/SystemApp架构设计.md §2。本模块是纯框架层：
不 import 任何业务组件（组合根在 system_app.py，组件实现各自模块内）。

生命周期钩子执行约定（SystemApp 广播，仅 4 个钩子）：
- 启动方向（on_init → async_before_start → async_after_start）：按组件**注册顺序**串行执行——
  注册顺序即依赖顺序（如 ToolRegistry 必须先于 AgentRegistry 装载）。
  on_init 在 init_app() 同步阶段广播；async_before_start 在 start_app() 事件循环内广播（主启动钩子）；
  async_after_start 在全部组件的 async_before_start 完成后广播（依赖全体就绪的收尾动作，如观测汇总）。
- 关闭方向（async_before_stop）：在 stop_app() 内按注册**逆序**执行，
  异常逐组件隔离并记录，保证全部组件都有机会收尾。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Optional, Type, TypeVar, Union

from shared import log


class LifeCycle:
    """组件生命周期钩子定义（仅 4 个；重写需要的即可，其余空实现）。"""

    def on_init(self):
        """组件初始化（init_app() 同步阶段广播；轻量装配/同步装载如目录扫描）。"""

    async def async_before_start(self):
        """启动主钩子（start_app() 事件循环内广播：连接池/后台任务/预热）。"""

    async def async_after_start(self):
        """启动收尾钩子（全部组件 async_before_start 完成后广播：依赖全体就绪的观测/校验）。"""

    async def async_before_stop(self):
        """关闭主钩子（stop_app() 逆序广播：后台任务/连接池释放）。"""


class ComponentType(str, Enum):
    """组件名注册表：name 用枚举，落库/日志统一转 .value 字符串。"""

    LLM_FACADE = "llm_facade"
    TOOL_REGISTRY = "tool_registry"
    Tool_EXECUTOR = "tool_executor"
    SKILL_MANAGER = "skill_manager"
    AGENT_REGISTRY = "agent_registry"
    SESSION_POOL = "session_pool"
    SESSION_PERSISTENCE = "session_persistence"
    TASK_BOARD = "task_board"
    MEMORY_MANAGER = "memory_manager"
    SESSION_SUMMARY = "session_summary"
    COMPACTION = "compaction"
    CHAT_MESSAGE_STORE = "chat_message_store"
    TRACE_MAINTENANCE = "trace_maintenance"
    MEMORY_SCHEDULER = "memory_scheduler"
    CHAT_SERVICE = "chat_service"
    MESSAGE_BUS = "message_bus"


_EMPTY_DEFAULT_COMPONENT = "_EMPTY_DEFAULT_COMPONENT"


class BaseComponent(LifeCycle, ABC):
    """组件基类：所有进程级组件继承并实现 init_app；生命周期钩子按需重写。"""

    name: Union[str, ComponentType] = "base_component"

    def __init__(self, system_app: Optional["SystemApp"] = None):
        if system_app is not None:
            self.init_app(system_app)

    @abstractmethod
    def init_app(self, system_app: "SystemApp"):
        """持有容器引用（组件内经 self.system_app / get_app() 访问其他组件）。"""

    @classmethod
    def get_instance(
        cls: Type[T],
        system_app: "SystemApp",
        default_component=_EMPTY_DEFAULT_COMPONENT,
        or_register_component: Optional[Type[T]] = None,
        *args,
        **kwargs,
    ) -> T:
        """按组件名取实例；未注册时可选落 default 或现场注册 or_register_component。"""
        if "default_component" in kwargs:
            raise ValueError("default_component argument given in both fixed and **kwargs")
        if "or_register_component" in kwargs:
            raise ValueError("or_register_component argument given in both fixed and **kwargs")
        kwargs["default_component"] = default_component
        kwargs["or_register_component"] = or_register_component
        return system_app.get_component(
            cls.name,
            cls,
            *args,
            **kwargs,
        )


T = TypeVar("T", bound=BaseComponent)


class SystemApp(LifeCycle):
    """全局容器：组件注册表 + 生命周期广播。

    广播顺序：启动按注册顺序、关闭按注册逆序（依赖即顺序，见模块 docstring）。
    类型化访问器由 system_app.py 的具体容器子类提供（组合根特权）。
    """

    def __init__(self) -> None:
        self.components: Dict[str, BaseComponent] = {}
        self._stop_event = threading.Event()
        self._stop_event.clear()

    # ------------------------------------------------------------ 注册

    def register(self, component: Type[T], *args, **kwargs) -> T:
        """按组件类注册：构造实例（传入容器）并登记。"""
        instance = component(self, *args, **kwargs)
        self.register_instance(instance)
        return instance

    def register_instance(self, instance: T) -> T:
        """登记一个已构造的组件实例（name 重复视为装配错误，fail fast）。"""
        name = instance.name
        if isinstance(name, ComponentType):
            name = name.value
        if name in self.components:
            raise RuntimeError(f"Component name {name} already exists: {self.components[name]}")
        log.info("Register component with name {} and instance: {}", name, instance)
        self.components[name] = instance
        instance.init_app(self)
        return instance

    def get_component(
        self,
        name: Union[str, ComponentType],
        component_type: Type,
        default_component=_EMPTY_DEFAULT_COMPONENT,
        or_register_component: Optional[Type[T]] = None,
        *args,
        **kwargs,
    ) -> T:
        """按组件名取实例；缺失时可选落 default / 现场注册，否则抛错（fail fast）。"""
        if isinstance(name, ComponentType):
            name = name.value
        component = self.components.get(name)
        if not component:
            if or_register_component:
                return self.register(or_register_component, *args, **kwargs)
            if default_component != _EMPTY_DEFAULT_COMPONENT:
                return default_component
            raise ValueError(f"No component found with name {name}")
        if not isinstance(component, component_type):
            raise TypeError(f"Component {name} is not of type {component_type}")
        return component

    # ------------------------------------------------------------ 生命周期广播（启动：注册顺序）

    def on_init(self):
        """按注册顺序调用全部组件的 on_init。"""
        for component in list(self.components.values()):
            component.on_init()

    async def async_before_start(self):
        """按注册顺序 await 全部组件的 async_before_start（启动主钩子）。"""
        for component in list(self.components.values()):
            await component.async_before_start()

    async def async_after_start(self):
        """按注册顺序 await 全部组件的 async_after_start（全部预热完成后的收尾钩子）。"""
        for component in list(self.components.values()):
            await component.async_after_start()

    # ------------------------------------------------------------ 生命周期广播（关闭：注册逆序）

    async def async_before_stop(self):
        """按注册逆序 await 全部组件的 async_before_stop；异常逐组件隔离，进程级幂等。"""
        if self._stop_event.is_set():
            return
        for component in reversed(list(self.components.values())):
            try:
                await component.async_before_stop()
            except Exception as exc:
                log.error("component {} async_before_stop 失败（继续其余组件收尾）: {}",
                          getattr(component, "name", component), exc)
        self._stop_event.set()
