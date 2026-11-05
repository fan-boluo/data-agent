import asyncio
import importlib
import inspect
import json
import sys
from abc import ABC
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.tools.base import Tool, BaseTool
from codegenx.ai_service.tools.file import WriteFileTool, ReadFileTool
from codegenx.ai_service.utils.config import config
from shared import log

# 工具目录
BUILTIN_TOOLS_DIR = Path(__file__).parent.parent / "tools"

class ToolRegistry(BaseComponent):
    """启动扫描一次的全局工具注册表（进程级组件，子代理用 child_view 出过滤视图）。"""

    name = ComponentType.TOOL_REGISTRY

    def __init__(self, system_app=None):
        BaseComponent.__init__(self, system_app)
        self.config = config
        self.tools: list[Tool] = []
        self.regist_tools()

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    def regist_tools(self):
        """
        加载所有的工具（注册时即排除 config.tools.excluded 中不启用的工具）
        """
        classes = []
        pattern = "*.py"

        # 遍历所有 .py 文件
        for file_path in BUILTIN_TOOLS_DIR.glob(pattern):
            if file_path.name.startswith("_"):
                continue  # 跳过 __init__.py 等
            # 拼接模块名
            relative_path = file_path.relative_to(BUILTIN_TOOLS_DIR)
            module_name = relative_path.with_suffix("").as_posix().replace("/", ".")
            try:
                # 导入模块
                module = importlib.import_module(f"codegenx.ai_service.tools.{module_name}")
                # log.debug(f"导入模块成功: {module_name}")
                # 遍历模块里所有成员
                for name, obj in vars(module).items():
                    # 筛选：是类 + 继承自 BaseTool + 不是自身 + 不是抽象基类
                    if (
                            isinstance(obj, type)  # 是类
                            and issubclass(obj, BaseTool)  # 继承 BaseTool
                            and obj is not BaseTool  # 排除基类自身
                            and not inspect.isabstract(obj)  # 排除抽象基类（SemanticTool 等）
                    ):
                        classes.append(obj)
                        # log.debug(f"✅ 找到工具类: {module_name}.{name}")

            except Exception as e:
                log.error(f"导入失败 {file_path}: {e}")
        # 注册时即排除不启用的工具（config.tools.excluded），build_tool 直接构建无需再过滤
        excluded_names = {str(n).strip() for n in (self.config.tools.excluded or [])}
        for tool_cls in classes:
            try :
                tool_instance = tool_cls()
                name = getattr(tool_instance, "name", None)
                if name in excluded_names:
                    log.info(f"工具 {name} 在 tools.excluded 名单中，跳过注册")
                    continue
                label = getattr(tool_instance, "label", None)
                description = getattr(tool_instance, "description", None)
                parameters = getattr(tool_instance, "parameters", None)
                if not all([name, description, parameters]):
                    log.warning(f"⚠️ 工具 {tool_cls.__name__} 缺少必填字段，跳过")
                    continue
                tool = Tool(
                    name=name,
                    label=label,
                    description=description,
                    parameters=parameters,
                    executor=tool_instance.execute,
                    obs_category=str(getattr(tool_instance, "obs_category", "tool") or "tool"),
                )
                existing_tool = next((t for t in self.tools if t.name == name), None)
                if existing_tool is None:
                    self.tools.append(tool)
                    log.info(f"注册工具成功: {name}")
                else:
                    log.info(f"工具已注册跳过: {name}")

            except Exception as e:
                log.error(f"❌ 初始化工具失败 {tool_cls.__name__}: {str(e)}")

        log.info(f"\n🎉 工具加载完成，总计加载: {len(self.tools)} 个工具")


    def child_view(self, excluded: set[str] | None = None,
                   allowed: list[str] | None = None) -> "ToolRegistry":
        """产出过滤子视图：浅拷贝 tool 列表，不重扫目录（P3，docs/SystemApp架构设计.md §6）。

        excluded 先剔除（如 subagent/compact），allowed 再白名单裁剪（None=不裁剪）。
        返回的 ToolRegistry 与父注册表共享同一批 Tool 对象（executor 等只读复用）。
        """
        view = ToolRegistry.__new__(ToolRegistry)  # 绕过 __init__ 的目录扫描
        view.tools = list(self.tools)
        if excluded:
            view.tools = [t for t in view.tools if t.name not in excluded]
        if allowed:
            allowed_set = {str(n).strip() for n in allowed if str(n).strip()}
            view.tools = [t for t in view.tools if t.name in allowed_set]
        return view

    def call_tool(self, tool_name: str, tool_input: dict) -> Any:
        """
        标准化工具调用函数
        """
        func = None
        for tool in self.tools:
            if tool_name == tool.name:
                func = tool.executor
        if func:
            log.info(f"执行工具：{tool_name}")
            return func(**tool_input)
        else:
            return {"error": f"未知工具：{tool_name}"}

    def build_tool(self) -> list[dict]:
        """构建全量 function-calling 工具目录（不启用的工具已在注册时排除）。

        原为 async def 但体内无 await，改同步以适配 _resolve_tool_catalog 同步链路
        （BaseReActAgent._prepare_agent 经 _resolve_tool_catalog 取目录）。
        """
        tool_catalog = self.tools

        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tool_catalog or []
        ]

    

    def build_agent_tools(self, allowlist: list[str] | None = None) -> list[dict]:
        """按白名单构建 agent 的 function-calling 工具目录。

        白名单非空 → 仅构建白名单内工具；空/None 表示该 agent 不启用工具（返回空列表）。
        白名单来自 config.json agents[].tools，供 BaseReActAgent._prepare_agent 调用。
        """
        if not allowlist:
            return []
        names = {str(n).strip() for n in allowlist if str(n).strip()}
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools if tool.name in names
        ]


def get_tool_registry() -> ToolRegistry:
    """取全局工具注册表组件（经容器查表；须在 initialize_components 注册 tools 之后）。"""
    from codegenx.ai_service.system_app import get_app

    return ToolRegistry.get_instance(get_app())


def initialize_tools(system_app) -> ToolRegistry:
    """注册工具注册表组件（system_app.initialize_components 调用；构造即扫描目录）。"""
    return system_app.register(ToolRegistry)



if __name__ == '__main__':
    write_params = {"path": "test_write_tool.txt",
              "content": "aaaaaaaaaaaaa\naj急急急安51234rf!@#$%^^&&*(())_"
              }


    async def test_write():
        write_tool = WriteFileTool()
        write_tool.validate_params(write_params)
        result = await write_tool.execute(write_params)
        print(result)


    read_params = {"path": "test_write_tool.txt",
                   }
    async def test_read():
        tool = ReadFileTool()
        tool.validate_params(read_params)
        result = await tool.execute(read_params)
        print(result)

    # asyncio.run(test_read())

    handler = ToolRegistry()
    handler.regist_tools()  # 二次执行