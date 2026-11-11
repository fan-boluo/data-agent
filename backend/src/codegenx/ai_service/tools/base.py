import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

from pydantic import BaseModel

from shared import log
from codegenx.ai_service.hook import HookContext, HookDecision, HookEvent, on


class Tool(BaseModel):
    name: str
    label: str  # 类别
    description: str
    parameters: dict[str, Any]  # 工具执行的参数
    executor: Callable  # 调用的函数
    obs_category: str = "tool"  # 观测类别（tool/retrieval）：trace span 的 op_type


class ToolResult(BaseModel):
    success: bool  # 工具执行成果的标志
    data: str=""  # 返回给llm的数据
    message:str = ""  # 失败原因
    render:str= ""  # 展示用
    extras: dict[str, Any] = {}  # 追踪/观测用附加数据（不进 LLM 上下文，由 hook 消费）
    error_class: str = ""  # 失败分类（执行器透传给监控指标；空=按消息自动判定）


@dataclass(frozen=True)
class ToolSession:
    """工具可读的会话上下文：只凭 session_id 解析，业务参数不进入工具入参。

    执行器仅向 params 注入 session_id；工具按需自行取 app_id/user_id/当前数据集，
    不再由外部塞入业务参数（工具保持"只做一件事"的纯粹性）。
    """

    session_id: str = ""
    request_id: str = ""
    app_id: str = "main"
    user_id: str = ""
    current_db: str | None = None  # 形如 datasource.schema

    @classmethod
    async def load(cls, params: dict[str, Any]) -> "ToolSession":
        """按 params["session_id"] 从 SessionPool 取会话；无会话/无 id 时返回默认值。"""
        session_id = str(params.get("session_id") or "")
        if not session_id:
            return cls()
        from codegenx.ai_service.system_app import get_app

        state = await get_app().session_pool.get(session_id)
        if state is None:
            return cls(session_id=session_id)
        return cls(
            session_id=session_id,
            request_id=state.request_id,
            app_id=state.app_id,
            user_id=state.user_id,
            current_db=state.current_db,
        )

    @property
    def db_name(self) -> str:
        return self.current_db.split(".", 1)[0] if self.current_db and "." in self.current_db else ""

    @property
    def db_schema(self) -> str:
        return self.current_db.split(".", 1)[1] if self.current_db and "." in self.current_db else ""

    def workspace(self) -> Path:
        """用户项目目录（文件类工具的工作根）。"""
        from shared.constants import get_code_dir

        return get_code_dir(self.user_id or "main", self.app_id or "main")


class BaseTool(ABC):

    @property
    @abstractmethod
    def label(self) -> str:
        """
        类别
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def obs_category(self) -> str:
        """观测类别：语义层检索工具覆盖为 "retrieval"（trace 区分召回质量与工具执行）。"""
        return "tool"

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:  # 工具执行的参数
        ...

    @abstractmethod
    async def execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
            # on_update: Callable[[AgentToolResult[TDetails]], None] | None = None,
    ) -> ToolResult:
        """
        params:入参
        signal:终止信号
        on_update:???
        """
        ...

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")

        required = schema.get("required", [])
        for param in required:
            if param not in params:
                raise ValueError(f"Missing required parameter: {param}")

        # Basic type checking (simplified)
        properties = schema.get("properties", {})
        for key, value in params.items():
            if key not in properties:
                log.warning(f"Unknown parameter: {key}")
                continue

            prop_schema = properties[key]
            expected_type = prop_schema.get("type")

            # Basic type validation
            if expected_type == "string" and not isinstance(value, str):
                raise ValueError(f"Parameter {key} must be string, got {type(value).__name__}")
            elif expected_type == "integer" and not isinstance(value, int):
                raise ValueError(f"Parameter {key} must be integer, got {type(value).__name__}")
            elif expected_type == "number" and not isinstance(value, (int, float)):
                raise ValueError(f"Parameter {key} must be number, got {type(value).__name__}")
            elif expected_type == "boolean" and not isinstance(value, bool):
                raise ValueError(f"Parameter {key} must be boolean, got {type(value).__name__}")
            elif expected_type == "array" and not isinstance(value, list):
                raise ValueError(f"Parameter {key} must be array, got {type(value).__name__}")
            elif expected_type == "object" and not isinstance(value, dict):
                raise ValueError(f"Parameter {key} must be object, got {type(value).__name__}")

        return params


# ── Hook 监听器：工具参数守卫接入事件总线（docs/Hook设计.md §4.2） ───────────
# 文件工具集合：这些工具依赖有效的 path 参数
_FILE_TOOLS = {"write_file", "read_file", "edit_file", "delete_file"}


@on(HookEvent.BEFORE_TOOL_CALL, name="file_tool_param_guard", priority=20)
async def file_tool_param_guard(ctx: "HookContext") -> "HookDecision | None":
    """文件工具参数守卫（迁自 runtime._execute_step 内联校验）：

    path 为空直接 blocked；write_file 额外要求 content 非空。
    """
    tool_call = ctx.data.get("tool_call") or {}
    tool_name = tool_call.get("name", "")
    if tool_name not in _FILE_TOOLS:
        return None
    args = tool_call.get("arguments") or {}
    path = (args.get("path") or "").strip() if isinstance(args, dict) else ""
    content = (args.get("content") or "").strip() if isinstance(args, dict) else ""

    if not path:
        return HookDecision.block(
            f"❌ {tool_name} 调用失败: path 参数为空。\n"
            f"必须提供有效的文件路径，例如: path=\"train_model.py\"\n"
            f"如果需要了解目录结构，请先用 list_directory 查看。"
        )
    if tool_name == "write_file" and not content:
        return HookDecision.block(
            f"❌ write_file 调用失败: content 参数为空。\n"
            f"write_file 需要同时提供 path 和 content 两个参数。\n"
            f"请将完整的文件源代码填入 content 参数后重新调用 write_file。"
        )
    return None
