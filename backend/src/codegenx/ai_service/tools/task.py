"""Task management tools (s12, P2 服务化改写 docs/SystemApp架构设计.md §4.3)。

Four tools that give the LLM full CRUD access to the persistent task board:
  task_create  — create a new task, optionally with dependencies
  task_update  — change status / executor_agent / subject / description
  task_get     — fetch a single task record
  task_list    — list all (or filtered) tasks on the board

工具内部经 get_app().tasks（全局 TaskBoardService，ids 作方法参数）。
任务看板按 request 维度存储：ids 中的归属信息由工具凭 session_id 自行解析，
task_id 为 request 内局部 id，跨请求互不可见。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log


async def _scope(params: dict) -> tuple[Any | None, dict]:
    """取全局任务看板服务与会话 ids；无会话时服务为 None（由调用方提示）。"""
    sess = await ToolSession.load(params)
    if not sess.session_id:
        return None, {}
    from codegenx.ai_service.system_app import get_app

    # request_id 一起下传：任务按 request 单文件隔离，task_id 为 request 内局部 id
    return get_app().tasks, {
        "user_id": sess.user_id,
        "app_id": sess.app_id,
        "session_id": sess.session_id,
        "request_id": sess.request_id,
    }


class TaskCreateTool(BaseTool):
    @property
    def name(self) -> str:
        return "task_create"

    @property
    def label(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Create a new task on the persistent task board. "
            "Use depends_on to specify task IDs that must complete before this task can start. "
            "Returns the created TaskRecord."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "One-line description of the task.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional longer explanation or acceptance criteria.",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "IDs of tasks that must complete before this task can start.",
                },
            },
            "required": ["subject"],
        }

    async def execute(self, params: dict, signal: asyncio.Event | None = None) -> ToolResult:
        tasks, ids = await _scope(params)
        if tasks is None:
            return ToolResult(success=False, data="Task board is not available (missing session ids).")
        try:
            task = tasks.create(
                subject=str(params.get("subject", "")),
                description=str(params.get("description", "") or ""),
                depends_on=list(params.get("depends_on") or []),
                **ids,
            )
            return ToolResult(
                success=True,
                data=task.model_dump_json(),
                render=json.dumps({"action": "create", "task": task.model_dump(mode="json")}, ensure_ascii=False),
            )
        except Exception as exc:
            log.error("[task_create] {}", exc)
            return ToolResult(success=False, message=f"Error: {exc}", render=f"{self.name} 任务创建失败")


class TaskUpdateTool(BaseTool):
    @property
    def name(self) -> str:
        return "task_update"

    @property
    def label(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Update a task's status, executor_agent, subject, or description. "
            "Setting status to 'completed' automatically unblocks dependent tasks. "
            "Valid statuses: pending, in_progress, completed, deleted."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID of the task to update."},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "deleted"],
                    "description": "New status for the task.",
                },
                "executor_agent": {"type": "string", "description": "agent_name of the executor agent working on this task."},
                "subject": {"type": "string", "description": "Updated one-line subject."},
                "description": {"type": "string", "description": "Updated description."},
            },
            "required": ["task_id"],
        }

    async def execute(self, params: dict, signal: asyncio.Event | None = None) -> ToolResult:
        tasks, ids = await _scope(params)
        if tasks is None:
            return ToolResult(success=False, data="Task board is not available (missing session ids).")
        task_id = params.get("task_id")
        if task_id is None:
            return ToolResult(success=False, data="task_id is required.")
        try:
            task = tasks.update(
                int(task_id),
                status=params.get("status"),
                executor_agent=params.get("executor_agent"),
                subject=params.get("subject"),
                description=params.get("description"),
                **ids,
            )
            return ToolResult(
                success=True,
                data=task.model_dump_json(),
                render=json.dumps({"action": "update", "task": task.model_dump(mode="json")}, ensure_ascii=False),
            )
        except Exception as exc:
            log.error("[task_update] {}", exc)
            return ToolResult(success=False, message=f"Error: {exc}", render=f"{self.name} 任务更新失败")


class TaskGetTool(BaseTool):
    @property
    def name(self) -> str:
        return "task_get"

    @property
    def label(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return "Fetch a single task record by ID. Returns the full TaskRecord including blocked_by and blocks lists."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "ID of the task to fetch."},
            },
            "required": ["task_id"],
        }

    async def execute(self, params: dict, signal: asyncio.Event | None = None) -> ToolResult:
        tasks, ids = await _scope(params)
        if tasks is None:
            return ToolResult(success=False, data="Task board is not available (missing session ids).")
        task_id = params.get("task_id")
        if task_id is None:
            return ToolResult(success=False, data="task_id is required.")
        try:
            task = tasks.get(int(task_id), **ids)
            if task is None:
                return ToolResult(success=False, data=f"Task {task_id} not found.", render=f"任务 #{task_id} 不存在")
            return ToolResult(success=True, data=task.model_dump_json(), render=f"获取任务: {task.subject}")
        except Exception as exc:
            log.error("[task_get] {}", exc)
            return ToolResult(success=False, message=f"Error: {exc}", render=f"{self.name} 任务获取失败")


class TaskListTool(BaseTool):
    @property
    def name(self) -> str:
        return "task_list"

    @property
    def label(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "List all tasks of the current request on the board. Optionally filter by status. "
            "Returns an array of TaskRecords and a text summary of the board."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "deleted"],
                    "description": "Filter by status. Omit to list all tasks.",
                },
            },
        }

    async def execute(self, params: dict, signal: asyncio.Event | None = None) -> ToolResult:
        tasks, ids = await _scope(params)
        if tasks is None:
            return ToolResult(success=False, data="Task board is not available (missing session ids).")
        try:
            status_filter = params.get("status")
            board_tasks = tasks.list_all(status=status_filter, **ids)
            board = tasks.get_board(**ids)
            return ToolResult(
                success=True,
                data=str({"tasks": [t.model_dump(mode="json") for t in board_tasks], "board": board}),
                render=f"任务列表: {len(board_tasks)} 个任务"
            )
        except Exception as exc:
            log.error("[task_list] {}", exc)
            return ToolResult(success=False, message=f"Error: {exc}")
