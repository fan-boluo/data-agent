"""
CodeCheckTool — 纯静态语法检查，零执行风险。
用 ast.parse 检查 Python 文件语法，返回逐行错误信息。
安全可自动注册（继承 BaseTool）。
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from codegenx.ai_service.tools.file import resolve_read_path
from shared import log

# 可根据文件扩展名检查的文件类型
SUPPORTED_SUFFIXES = frozenset({".py"})


def _check_file(path: Path) -> tuple[bool, list[str]]:
    """对单个文件做 AST 语法检查。返回 (is_valid, [error_lines])。"""
    try:
        source = path.read_text(encoding="utf-8")
    except Exception as e:
        return False, [f"无法读取文件: {e}"]

    if not source.strip():
        return True, ["(空文件)"]

    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as e:
        lines = source.split("\n")
        line_no = e.lineno or 1
        col = e.offset or 0
        context_lines: list[str] = []
        # 显示出错行及上下各 2 行
        start = max(0, line_no - 3)
        end = min(len(lines), line_no + 2)
        for i in range(start, end):
            prefix = f"{i + 1:>5} | "
            context_lines.append(prefix + lines[i].rstrip())
        error_msg = (
            f"  SyntaxError: {e.msg}\n"
            f"  位置: 第 {line_no} 行, 第 {col} 列\n"
            + "\n".join(context_lines)
        )
        return False, [error_msg]
    except Exception as e:
        return False, [f"解析异常: {type(e).__name__}: {e}"]

    return True, []


class CodeCheckTool(BaseTool):

    @property
    def name(self) -> str:
        return "code_check"

    @property
    def label(self) -> str:
        return "system"

    @property
    def description(self) -> str:
        return (
            "静态检查 Python 代码语法是否正确（仅 AST 解析，不执行任何代码）。\n\n"
            "## 用途\n"
            "- 验证刚编写的 .py 文件是否有语法错误\n"
            "- 在 write_file / edit_file 后快速确认代码结构正确\n"
            "- 发现 SyntaxError 并定位到具体行号和列号\n\n"
            "## 安全保证\n"
            "- 纯静态分析：只用 ast.parse 解析语法树，不 import 任何模块，不执行代码\n"
            "- 零副作用：不创建进程、不写文件、不访问网络\n"
            "- 适用于任何 .py 文件，包括未完成或有外部依赖的代码"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要检查的 Python 文件路径（.py）",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        params: dict,
        signal: asyncio.Event | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("code_check 执行异常: {}", exc)
            return ToolResult(success=False, message=f"检查失败: {exc}", render=f"语法检查失败: {params.get('path', '')}")

    async def _do_execute(
        self,
        params: dict,
        signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path_str: str = (params.get("path") or "").strip()
        if not path_str:
            return ToolResult(
                success=False,
                message="缺少 path 参数。请提供要检查的 Python 文件路径。",
                render=f"{self.name} 执行失败: 缺少 path 参数",
            )
        sess = await ToolSession.load(params)
        file_path = resolve_read_path(path_str, sess.app_id, sess.user_id)
        file_path = file_path.resolve()

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if not file_path.exists():
            return ToolResult(success=False, message=f"文件不存在: {file_path}",render=f"{self.name} 文件不存在 {file_path.name}")

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            return ToolResult(
                success=False,
                message=f"不支持的文件类型 '{suffix}'，当前仅支持: {', '.join(sorted(SUPPORTED_SUFFIXES))}",
                render=f"{self.name} 不支持的文件类型 '{suffix}"
            )

        is_valid, errors = await asyncio.get_event_loop().run_in_executor(
            None, _check_file, file_path
        )

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if is_valid:
            source = file_path.read_text(encoding="utf-8")
            line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
            return ToolResult(
                success=True,
                data=f"语法检查通过: {file_path} ({line_count} 行, {len(source)} 字符)",
                render=f"语法检查通过: {file_path.name} ({line_count} 行, {len(source)} 字符)",
            )
        else:
            return ToolResult(
                success=False,
                data="\n\n".join(errors),
                message=f"语法错误: {file_path} 存在 {len(errors)} 个语法错误",
                render=f"语法错误: {file_path.name} 存在 {len(errors)} 个语法错误",
            )
