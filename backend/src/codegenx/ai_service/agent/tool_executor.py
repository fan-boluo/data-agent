import asyncio
import inspect
import json
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.tools.base import ToolResult
from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState, TurnStoppedError
from codegenx.ai_service.hook import HookContext, HookDecision, HookEvent, on, hook_manager
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.trace import  start_span
from codegenx.ai_service.trace.metrics import feed_tool_call
from codegenx.ai_service.utils.config import config
from shared import log
from shared.constants import get_code_dir

if TYPE_CHECKING:
    from codegenx.ai_service.system_app import SystemApp

DATA_ANALYSIS_TOOL_NAMES = {
    "describe_csv",
    "sample_csv_rows",
    "describe_csv_stats",
    "guess_analysis_task",
    "execute_postgresql",
}


TASK_TOOL_NAMES = {"task_create", "task_update", "task_get", "task_list"}

# 长耗时可并行工具（如 dispatch_agent 派发子智能体）：同轮多次调用 = 并行执行
# （gather + multi_agents.max_parallel 信号量限流）；其余工具保持串行
# （run_python 等共享执行容器，并行会冲突）
PARALLEL_TOOL_NAMES = {"dispatch_agent"}


# ── 路径安全守卫（无状态函数 + 模块级监听器，docs/Hook设计.md §4.2）──────────
# 注意：@on 装饰时注册裸函数对象（分发器直接 callback(ctx)），
# 监听器必须是无 self 的模块级函数，否则每次分发都 TypeError。


def _resolve_safe_paths(session_state: Any | None) -> list[Path]:
    """从会话状态解析 safe_paths"""
    context = getattr(session_state, "context_manager", None) if session_state is not None else None
    if not context:
        return []
    return [Path(path).resolve() for path in context.metadata.safety_path]


def _is_safe_path(target: Path, safe_paths: list[Path]) -> bool:
    for sp in safe_paths:
        try:
            # 能够计算 relatively 说明 target 在 sp 之内
            target.relative_to(sp)
            return True
        except ValueError:
            pass
    return False


def _resolve_candidate_path(value: str, app_id: str | int, user_id: str | int = "") -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = get_code_dir(user_id or "main", app_id) / candidate
    return candidate.resolve()


@on(HookEvent.BEFORE_TOOL_CALL, name="safe_path_guard", priority=30)
async def safe_path_guard(ctx: "HookContext") -> "HookDecision | None":
    """路径安全守卫（迁自 ToolExecutor._perform_safety_checks，安全边界前置）。

    检查路径类参数是否在 safe_path 内，越界返回 HookDecision.block 短路拒绝；
    并对 Bash 类命令做基本危险命令拦截。
    与 tools/base.py 的 file_tool_param_guard（参数空值校验）分工不同，故命名区分。
    """
    tool_call = ctx.data.get("tool_call") or {}
    tool_name = str(tool_call.get("name") or "")
    tool_input = dict(tool_call.get("arguments") or {})
    if not tool_name or not tool_input:
        return None

    session_state = ctx.session
    safe_paths = _resolve_safe_paths(session_state)
    user_id = str(getattr(session_state, "user_id", "") or "") if session_state is not None else ""
    request = getattr(session_state, "request", None) if session_state is not None else None
    app_id = str(getattr(request, "app_id", "") or "") if request is not None else ""
    app_id = app_id or str(tool_input.get("app_id", "main"))

    for key, value in tool_input.items():
        # 路径类参数严格校验（文件操作工具的参数可能叫 path/filename/src/dest 等）
        if key in ("path", "filename", "src", "dest", "file_path"):
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                target_path = _resolve_candidate_path(value, app_id, user_id)
                if not _is_safe_path(target_path, safe_paths):
                    return HookDecision.block(
                        f"{tool_name}：越界访问：目标路径 '{target_path}' 不在 safe_path 允许范围内"
                    )
            except Exception as e:
                return HookDecision.block(f"{tool_name}：路径解析错误：{e}")

    # 对于 bash 类工具，进一步做危险命令黑名单拦截（示例策略）
    if tool_name in ("run_bash", "bash", "execute_command"):
        cmd = str(tool_input.get("command", ""))
        # 此处可以做命令黑名单或敏感命令策略...
        forbidden_cmds = ("rm -rf /", "mkfs", "chown")
        if any(forbidden in cmd for forbidden in forbidden_cmds):
            return HookDecision.block(
                f"{tool_name}：命中了危险命令策略：不能执行可能破坏系统的命令"
            )

    return None

class ToolExecutor(BaseComponent):
    """工具执行器：统一执行 LLM 返回的 tool_calls，转换为可回填对话的 tool 消息。

    - dispatch_tool_calls：BaseReActAgent 工具化循环的调度入口，逐个执行并转换
    - execute：单工具执行入口，完成上下文注入（db_name/app_id/user_id 等）与注册表查找
    说明：路径安全等守卫为 BEFORE_TOOL_CALL 监听器，原由 runtime 在执行前分发；
    多 agent 链路暂不接 hook，工具内部已有边界保护（路径服务端解析、SQL 只读守卫）。
    """

    # 组件名：system_app.tools_executor 属性按此名取实例
    name = ComponentType.Tool_EXECUTOR

    def __init__(self, system_app: "SystemApp | None" = None):
        super().__init__(system_app)

    def init_app(self, system_app: "SystemApp") -> None:
        # 无状态服务类，仅持有容器引用（工具查找时经 get_app() 取注册表）
        self.system_app = system_app

    # 单个工具结果回填对话的最大字符数（防止撑爆上下文）
    MAX_TOOL_RESULT_CHARS = 8000

    async def dispatch_tool_calls(
        self, tool_calls: list[dict[str, Any]] | None,
        session_state: RuntimeSessionState | None = None,
    ) -> list[dict[str, Any]]:
        """执行 tool_calls 列表，返回 [{"role": "tool", ...}] 消息列表（与 tool_calls 顺序对应）。

        - PARALLEL_TOOL_NAMES 内的长耗时工具（如 dispatch_agent）同轮多次调用
          并行执行（gather + multi_agents.max_parallel 信号量限流），其余串行；
        - 同批参数完全相同的 tool_call 只执行一次、结果复用（防 LLM 重复调用）；
        - 停止信号（TurnStoppedError/CancelledError）透传终止请求，不吞成工具失败。
        """
        # 归一化：(name, arguments, tool_call_id, 去重 key)
        normalized: list[tuple[str, dict, str, str]] = []
        for tc in tool_calls or []:
            name = str(tc.get("name") or "")
            arguments = tc.get("arguments") or {}
            # 部分模型把 arguments 输出成 JSON 字符串，兜底解析
            if isinstance(arguments, str):
                with suppress(Exception):
                    arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                arguments = {}
            # 去重 key：参数序列化后剔除全部空白（仅空白差异视为同一调用）
            key = re.sub(r"\s+", "", name + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            normalized.append((name, arguments, str(tc.get("id") or ""), key))

        # 同批去重缓存：key → 执行结果（asyncio 单线程，闭包 dict 并发安全）
        cache: dict[str, ToolResult] = {}

        async def _run_one(name: str, arguments: dict, tc_id: str, key: str) -> ToolResult:
            if key in cache:
                return cache[key]
            try:
                result = await self.execute(
                    {"name": name, "arguments": arguments, "id": tc_id}, session_state)
            except (TurnStoppedError, asyncio.CancelledError):
                raise  # 停止信号传播终止请求
            except Exception as exc:
                log.error("工具 {} 执行异常: {}", name, exc)
                result = ToolResult(success=False, message=str(exc))
            cache[key] = result
            return result

        results_by_id: dict[str, ToolResult] = {}
        # 并行组：长耗时工具 gather 执行（信号量限流；停止信号经 gather 传播）
        parallel = [t for t in normalized if t[0] in PARALLEL_TOOL_NAMES]
        if parallel:
            semaphore = asyncio.Semaphore(max(1, int(config.multi_agents.max_parallel)))

            async def _one(item):
                async with semaphore:
                    return item[2], await _run_one(*item)

            for tc_id, result in await asyncio.gather(*(_one(i) for i in parallel)):
                results_by_id[tc_id] = result
        # 串行组：其余工具顺序执行
        for name, arguments, tc_id, key in normalized:
            if tc_id not in results_by_id:
                results_by_id[tc_id] = await _run_one(name, arguments, tc_id, key)

        # 统一转换为 tool 消息（按原 tool_calls 顺序）
        results: list[dict[str, Any]] = []
        for name, _arguments, tc_id, _key in normalized:
            result = results_by_id[tc_id]
            # 约定：仅按 success 字段判定执行状态；成功回传 data，失败优先回传原因
            content = result.data if result.success else (result.message or result.data or "工具执行失败")
            if len(content) > self.MAX_TOOL_RESULT_CHARS:
                content = content[: self.MAX_TOOL_RESULT_CHARS] + (
                    f"\n...(已截断，原长 {len(content)} 字符)"
                )
            log.info("工具执行完成: {} | success={} | content_len={}", name, result.success, len(content))
            results.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": content,
                # 内部记账字段：供调用方统计工具成败（回填对话前剔除，不进 LLM API）
                "_success": result.success,
            })
        return results

    async def execute(self, tool_call: dict[str, Any],
                      session_state: RuntimeSessionState | None = None) -> ToolResult:
        """
        统一执行工具的逻辑。

        BEFORE_TOOL_CALL / AFTER_TOOL_CALL 钩子由本方法直接分发：
        - BEFORE：serial + 可短路（safe_path_guard/file_tool_param_guard/sql_trace_start 等）；
          blocked 时直接返回 ToolResult(success=False)，不再查找/执行工具
        - AFTER：parallel + 异常隔离（语义落表/persist/sql_trace_end 等），仅观测上报，不影响 result
        - trace 观测直埋：工具执行 with start_span 包裹（op_type=注册表 obs_category）；
          span 只记执行骨架（谁在何时调了什么、多久、成败），语义字段
          （sql/recalled_tables 等）由 AFTER_TOOL_CALL 监听器落 telemetry.tool_semantics
        """
        tool_name = tool_call.get("name")
        tool_input = dict(tool_call.get("arguments", {}) or {})
        app_id = str(session_state.request.app_id) if session_state is not None and session_state.request is not None else "main"
        user_id = getattr(session_state, "user_id", "") if session_state is not None else ""
        session_id = getattr(session_state, "session_id", "") if session_state is not None else ""
        turn_id = getattr(session_state, "request_id", "") if session_state is not None else ""
        trace_id = getattr(session_state, "trace_id", "")
        stop_signal = getattr(session_state, "stop_signal", None) if session_state is not None else None

        # 项目级工具：需要 user_id/app_id 定位用户项目目录
        if tool_name in {"read_file", "write_file", "edit_file", "delete_file", "list_directory",
                         "code_check", "find", "grep", "run_python", "list_csv_files",
                         "describe_csv", "sample_csv_rows", "describe_csv_stats"}:
            tool_input.setdefault("app_id", app_id)
            tool_input.setdefault("user_id", user_id)

        if tool_name == "subagent":
            tool_input.setdefault("app_id", app_id)
            tool_input.setdefault("user_id", user_id)
            tool_input.setdefault("trace_id", trace_id)
            tool_input.setdefault("plan_summary", "")
            tool_input.setdefault("parent_session_id", session_id)
            tool_input.setdefault("parent_turn_id", turn_id)

        if tool_name in TASK_TOOL_NAMES:
            # 任务看板工具：注入 ids（含 request_id，任务按 request 单文件隔离）
            tool_input.setdefault("user_id", user_id)
            tool_input.setdefault("app_id", app_id)
            tool_input.setdefault("session_id", session_id)
            tool_input.setdefault("request_id", turn_id)

        if tool_name in DATA_ANALYSIS_TOOL_NAMES:
            # agent 只聚焦用户当前选择的数据集：db_name/schema 全部来自 current_db
            # current_db 格式为 "datasource.schema"（如 business.dvdrental / tpcds_sf1.public）
            current_db = getattr(session_state, "current_db", None) if session_state is not None else None
            if current_db and "." in current_db:
                db_name, db_schema = current_db.split(".", 1)
                tool_input.setdefault("db_name", db_name)
                tool_input.setdefault("db_schema", db_schema)
            tool_input.setdefault("app_id", app_id)

        # BEFORE_TOOL_CALL 钩子（参数注入后触发，让守卫看到完整参数；不修改调用方原 tool_call）
        # turn 取自 session_state.activate_turn（monitor/sql_trace 等监听器依赖 ctx.turn）
        turn = getattr(session_state, "activate_turn", None) if session_state is not None else None
        before_ctx = HookContext(event=HookEvent.BEFORE_TOOL_CALL, session=session_state, turn=turn)
        before_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        await hook_manager.dispatch(HookEvent.BEFORE_TOOL_CALL, before_ctx)
        if before_ctx.action == "blocked":
            log.info("工具 {} 被 BEFORE_TOOL_CALL 钩子阻断: {}", tool_name, before_ctx.message)
            return ToolResult(success=False, message=before_ctx.message or "Tool blocked by hook")

        # 1. 查找工具
        tool = next((t for t in get_app().tool_registry.tools if t.name == tool_name), None)
        if not tool:
            log.error(f"Unknown tool called: {tool_name}")
            return ToolResult(success=False, message=f"未知工具：{tool_name}")

        # 2. 工具执行（trace 直埋：注册表 obs_category 决定 op_type——tool/retrieval；
        #    with 块天然维护 parent 栈，工具内派发的子 agent/llm span 挂本 span 下）
        func = tool.executor
        call_kwargs = {"params": tool_input}
        if "signal" in inspect.signature(func).parameters:
            call_kwargs["signal"] = stop_signal
        # 声明 session_state 参数的工具自动注入完整会话状态
        # （如 dispatch_agent：需要停止信号、看板握手、结果入史等会话能力）
        if "session_state" in inspect.signature(func).parameters:
            call_kwargs["session_state"] = session_state
        t0 = time.monotonic()
        # tool span input 携带 LLM 调用 id（协议溯源，与 llm span output.tool_calls[].id 对账）
        span_input = {"tool_call_id": str(tool_call.get("id") or ""), **tool_input}
        async with start_span(str(tool.obs_category or "tool"), str(tool_name),
                              input_data=span_input) as span:
            if inspect.iscoroutinefunction(func):
                result = await func(**call_kwargs)
            else:
                result = func(**call_kwargs)
            # 骨架收尾：输出概要 + 成败（语义字段由 AFTER 监听器单独落表）
            message = str(getattr(result, "message", "") or "")
            span.set_output({
                "success": bool(result.success),
                "message": message,
                "data": str(getattr(result, "data", "") or ""),
            })
            span.set_status(ok=bool(result.success), error=message or None)
            span_id = span.span["span_id"] if span.span else None
        feed_tool_call(app_id=session_state.app_id, session_id=session_state.session_id,tool_name=str(tool_name),
                       is_error=not result.success, latency_ms=(time.monotonic() - t0) * 1000)

        # AFTER_TOOL_CALL 钩子（parallel 容错分发：语义落表/落盘/记忆信号等）
        after_ctx = HookContext(event=HookEvent.AFTER_TOOL_CALL, session=session_state, turn=turn)
        after_ctx.data["tool_call"] = {**tool_call, "arguments": tool_input}
        after_ctx.data["result"] = result
        after_ctx.data["span_id"] = span_id
        await hook_manager.dispatch(HookEvent.AFTER_TOOL_CALL, after_ctx)

        return result


def initialize_tool_executor(system_app) -> ToolExecutor:
    """启动装配入口：注册工具执行器组件（DB-GPT 风格 initialize_xxx 约定）。"""
    return system_app.register(ToolExecutor)

