from __future__ import annotations

import asyncio
import base64
import difflib
import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal
import aiofiles
from pydantic import BaseModel, Field

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from codegenx.ai_service.tools.truncate_utils import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationResult,
    format_size,
    truncate_head,
)
from shared import log
from shared.constants import get_code_dir

class TextContent(BaseModel):
    """Text content block"""
    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    """Image content block"""
    type: Literal["image"] = "image"
    data: str  # Base64 or URL
    mime_type: str | None = Field(default=None, alias="mimeType")


# ── fuzzy matching helpers (from pi-tools edit_diff) ──

def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy matching: trailing whitespace, smart quotes, unicode dashes/spaces."""
    lines = [line.rstrip() for line in text.split("\n")]
    result = "\n".join(lines)
    result = result.replace("‘", "'").replace("’", "'").replace("‚", "'").replace("‛", "'")
    result = result.replace("“", '"').replace("”", '"').replace("„", '"').replace("‟", '"')
    for ch in "‐‑‒–—―−":
        result = result.replace(ch, "-")
    for ch in "            　":
        result = result.replace(ch, " ")
    return result


def _strip_bom(content: str) -> tuple[str, str]:
    if content.startswith("﻿"):
        return "﻿", content[1:]
    return "", content


def _detect_line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if crlf >= lf and crlf >= cr:
        return "\r\n"
    elif cr > lf:
        return "\r"
    return "\n"


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, ending: str) -> str:
    if ending == "\r\n":
        return text.replace("\n", "\r\n")
    elif ending == "\r":
        return text.replace("\n", "\r")
    return text


def _generate_diff(old_content: str, new_content: str) -> str:
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="original", tofile="modified", n=3))
    return "".join(diff)
def resolve_read_path(path: str, app_id: str | int = "main", user_id: str | int = ""):
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        expanded = get_code_dir(user_id or "main", app_id) / expanded
    resolved = expanded.resolve()

    return resolved


def check_path(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not path.is_file():
        if path.is_dir():
            raise IsADirectoryError(
                f"这是一个目录，不是文件: {path}。请使用 list_directory 工具浏览目录内容。"
            )
        raise ValueError(f"Not a regular file: {path}")

    if not os.access(path, os.R_OK):
        raise PermissionError(f"File not readable: {path}")


def detect_image_mime_type(path: Path):
    suffix = path.suffix.lower()

    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.svg': 'image/svg+xml',
    }

    return mime_types.get(suffix)


class ReadFileTool(BaseTool):

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def label(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. Supports text files and images "
            f"(jpg, png, gif, webp). Images are sent as attachments. "
            f"For text files, output is truncated to {DEFAULT_MAX_LINES} lines "
            f"or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first).\n\n"
            f"## Exploration strategy (especially for large files)\n"
            f"- grep first to find WHERE the interesting code is, then read_file with "
            f"offset/limit to see only the relevant lines\n"
            f"- Never read an entire file blindly — it wastes context and you'll miss "
            f"the key parts in the truncation noise\n"
            f"- Start with a small limit (50-100 lines) around the area of interest, "
            f"expand if needed\n\n"
            f"## Parameters\n"
            f"- offset: 1-indexed starting line number. Use when continuing from a "
            f"previous partial read or jumping to a known line\n"
            f"- limit: max lines to read. Always set this on large files — "
            f"without it the entire file is loaded and truncated, wasting tokens"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to read"
            },
            "offset": {
                "type": "integer",
                "description": "Starting line number (1-indexed). Use to skip ahead in large files or continue from a previous partial read."
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read. Always set this on files larger than 100 lines — prevents wasted tokens on truncation. Start small (50-100) and expand if needed."
            },
        },
        "required": ["path"]
    }

    async def read_file(self, path: str) -> bytes:
        """Read file contents"""
        async with aiofiles.open(path, 'rb') as f:
            return await f.read()

    async def execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
            # on_update: Callable[[AgentToolResult[TDetails]], None] | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("read_file 执行异常: {}", exc)
            return ToolResult(success=False, message=f"读取文件失败: {exc}",render=f"{self.name} 执行失败")

    async def _do_execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path = params.get("path", "")
        if not path:
            return ToolResult(
                success=False,
                message="缺少 path 参数。请提供要读取的文件路径，如 news_data/news.sohunews.010806.txt。可使用 list_directory 查看可用文件。",
                render=f"{self.name} 执行失败: 缺少 path 参数",
            )
        offset = params.get("offset")
        limit = params.get("limit")

        # Resolve path with macOS compatibility
        sess = await ToolSession.load(params)  # 工具只拿 session_id，归属信息自行解析
        absolute_path = resolve_read_path(path, sess.app_id, sess.user_id)

        # Enforce fs.workspaceOnly if configured
        check_path(absolute_path)

        # Check if already cancelled
        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        # Detect if image
        mime_type = detect_image_mime_type(absolute_path)

        if mime_type:
            # Read as image (binary)
            buffer = await self.read_file(str(absolute_path))
            base64_data = base64.b64encode(buffer).decode('utf-8')

            # Check if aborted after reading
            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

            # TODO: Implement image resizing if auto_resize_images is True
            # For now, just return the image as-is
            text_note = f"Read image file [{mime_type}]"

            return ToolResult(
                success=True,
                data=
                    TextContent(text=text_note).model_dump() +
                    ImageContent(data=base64_data, mime_type=mime_type).model_dump(),
                render = f"{self.name}, 执行结果：成功，path:{absolute_path.name}limit:{limit},offset:{offset}"
            )
        else:
            # Read as text file
            buffer = await self.read_file(str(absolute_path))
            text_content = buffer.decode('utf-8')
            all_lines = text_content.split('\n')
            total_file_lines = len(all_lines)

            # Check if aborted after reading
            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

            # Apply offset (1-indexed to 0-indexed)
            start_line = max(0, (offset - 1)) if offset else 0
            start_line_display = start_line + 1  # For display (1-indexed)

            # Check if offset is out of bounds
            if start_line >= total_file_lines:
                raise ValueError(
                    f"Offset {offset} is beyond end of file "
                    f"({total_file_lines} lines total)"
                )

            # Apply limit if specified
            user_limited_lines: int | None = None
            if limit is not None:
                end_line = min(start_line + limit, total_file_lines)
                selected_content = '\n'.join(all_lines[start_line:end_line])
                user_limited_lines = end_line - start_line
            else:
                selected_content = '\n'.join(all_lines[start_line:])

            # Apply truncation (respects both line and byte limits)
            truncation = truncate_head(selected_content, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES)

            output_text: str
            details: dict[str, Any] | None = None

            if truncation.first_line_exceeds_limit:
                # 首行超出，告诉llm部分读取
                first_line_size = format_size(
                    len(all_lines[start_line].encode('utf-8'))
                )
                output_text = (
                    f"[Line {start_line_display} is {first_line_size}, exceeds "
                    f"{format_size(DEFAULT_MAX_BYTES)} limit. Use bash: "
                    f"sed -n '{start_line_display}p' {path} | "
                    f"head -c {DEFAULT_MAX_BYTES}]"
                )
                details = {"truncation": truncation.__dict__}
            elif truncation.truncated:
                # Truncation occurred - build actionable notice
                end_line_display = start_line_display + truncation.output_lines - 1
                next_offset = end_line_display + 1

                output_text = truncation.content

                if truncation.truncated_by == "lines":
                    output_text += (
                        f"\n\n[Showing lines {start_line_display}-{end_line_display} "
                        f"of {total_file_lines}. Use offset={next_offset} to continue.]"
                    )
                else:
                    output_text += (
                        f"\n\n[Showing lines {start_line_display}-{end_line_display} "
                        f"of {total_file_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                        f"Use offset={next_offset} to continue.]"
                    )

                details = {"truncation": truncation.__dict__}
            elif user_limited_lines is not None and start_line + user_limited_lines < total_file_lines:
                # User specified limit, there's more content, but no truncation
                remaining = total_file_lines - (start_line + user_limited_lines)
                next_offset = start_line + user_limited_lines + 1

                output_text = truncation.content
                output_text += (
                    f"\n\n[{remaining} more lines in file. "
                    f"Use offset={next_offset} to continue.]"
                )
            else:
                # No truncation, no user limit exceeded
                output_text = truncation.content

            return ToolResult(
                success=True,
                data=output_text + ("\n 以下是执行的details:" + str(details) if details else ""),
                render = f"{self.name}, 执行结果：成功，path:{absolute_path.name},limit:{limit},offset:{offset}"
            )


class WriteFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "write_file"

    @property
    def label(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file if it doesn't exist, "
            "overwrites if it does. Automatically creates parent directories.\n\n"
            "## When to use (NOT when to use edit_file)\n"
            "- Creating a brand-new file that doesn't exist yet\n"
            "- Complete rewrite of a file where every line changes\n"
            "- For ALL other modifications to existing files, use edit_file instead — "
            "it's safer (only changes what you specify) and cheaper (no need to send the whole file)\n\n"
            "## Important\n"
            "- Never use write_file for small changes to large files (>200 lines). "
            "Sending the entire file content wastes significant context tokens. "
            "Use edit_file with precise old_text matching instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file"
                },
            },
            "required": ["path", "content"]
        }
    async def execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
            # on_update: Callable[[AgentToolResult[TDetails]], None] | None = None,
    ) -> ToolResult:
        try:
            return await self._do_execute(params, signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("write_file 执行异常: {}", exc)
            return ToolResult(success=False, message=f"写入文件失败: {exc}",render=f"{self.name} 执行失败")

    async def _do_execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return ToolResult(
                success=False,
                message="缺少 path 参数。请提供要写入的文件路径，如 xml_parser.py。",
                render=f"{self.name} 执行失败: 缺少 path 参数",
            )
        if not content:
            return ToolResult(
                success=False,
                message="缺少 content 参数。请提供要写入的文件内容。",
                render=f"{self.name} 执行失败: 缺少 content 参数",
            )
        # 解析
        sess = await ToolSession.load(params)
        absolute_path = resolve_read_path(path, sess.app_id, sess.user_id)
        log.debug(f"解析后的完整写入路径为:{absolute_path}")
        # 创建父目录
        parant_path = absolute_path.parent
        parant_path.mkdir(parents=True, exist_ok=True)

        # Check if already cancelled
        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        # Write the file
        async with aiofiles.open(absolute_path, 'w', encoding='utf-8') as f:
            await f.write(content)

        # Check if aborted after writing
        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        return ToolResult(
            success=True,
            data=f"Successfully wrote {len(content)} bytes to {path}",
            render=f"{self.name} 写入成功 {absolute_path.name} {len(content)} bytes"
        )

class EditFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def label(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "Performs exact string replacements in files.\n\n"
            "## When to use\n"
            "- Modifying existing code — ALWAYS prefer edit_file over write_file when the file already exists\n"
            "- Renaming variables, functions, or classes across a file (use replace_all: true)\n"
            "- Fixing bugs, adjusting logic, updating parameters — any targeted change to existing code\n"
            "- Appending to the end of a file — use position: \"end\" (old_text can be empty)\n"
            "- write_file should ONLY be used for creating brand-new files or complete rewrites\n\n"
            "## How to construct old_text\n"
            "- Copy the exact text from the file — preserve tabs, spaces, indentation exactly\n"
            "- Include enough surrounding context (3-5 lines) so old_text is unique in the file\n"
            "- old_text must match character-for-character: same whitespace, same punctuation, same case\n"
            "- If the match fails, read the file again with offset/limit around the target area and verify whitespace\n\n"
            "## Parameters\n"
            "- file_path: absolute path to the file\n"
            "- old_text: exact string to find. When position is \"end\", this is ignored (pass empty string)\n"
            "- new_text: replacement text, or the content to append when position is \"end\"\n"
            "- replace_all: set to true to replace ALL occurrences (default: replace only the first match)\n"
            "- position: \"replace\" (default) for find-and-replace, \"end\" to append new_text to the end of the file"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "New text to insert"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false, only replaces first match)"
                },
                "position": {
                    "type": "string",
                    "enum": ["replace", "end"],
                    "description": "Operation mode: \"replace\" (default) for find-and-replace, \"end\" to append new_text to end of file"
                },
            },
            "required": ["path", "old_text", "new_text"]
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
            log.warning("edit_file 执行异常: {}", exc)
            return ToolResult(success=False, message=f"编辑文件失败: {exc}",render=f"{self.name} 执行失败")

    async def _do_execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path = params["path"]
        old_text = params["old_text"]
        new_text = params["new_text"]
        position = params.get("position", "replace")

        sess = await ToolSession.load(params)
        absolute_path = resolve_read_path(path, sess.app_id, sess.user_id)
        check_path(absolute_path)

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        async with aiofiles.open(absolute_path, 'r', encoding='utf-8') as f:
            raw_content = await f.read()

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if position == "end":
            # Append new_text to end of file (ensure newline separation)
            separator = "\n" if raw_content and not raw_content.endswith("\n") else ""
            new_content = raw_content + separator + new_text + "\n"
        else:
            # Fuzzy matching: normalize BOM, line endings, unicode for robust matching
            bom, content = _strip_bom(raw_content)
            original_ending = _detect_line_ending(content)
            normalized_content = _normalize_lf(content)
            normalized_old = _normalize_lf(old_text)
            normalized_new = _normalize_lf(new_text)

            # Try exact match first, then fuzzy
            idx = normalized_content.find(normalized_old)
            if idx < 0:
                fuzzy_content = _normalize_for_fuzzy(normalized_content)
                fuzzy_old = _normalize_for_fuzzy(normalized_old)
                idx = fuzzy_content.find(fuzzy_old)
                if idx < 0:
                    snippet = old_text[:80].replace('\n', '\\n')
                    message = (
                        f"Edit failed: old_text not found in {path}. "
                        f"Searched for ({len(old_text)} chars): \"{snippet}...\" "
                        f"Common causes: whitespace mismatch (tabs vs spaces), "
                        f"line ending differences, or the text was already modified. "
                        f"Re-read the file around the target area with offset/limit and copy the exact text."
                    )
                    return ToolResult(success=False, message=message)
                # Use fuzzy match position
                normalized_old = _normalize_for_fuzzy(normalized_old)

            # Count occurrences to guard against ambiguous replacements
            if params.get("replace_all"):
                new_content = normalized_content.replace(normalized_old, normalized_new)
            else:
                occurrences = normalized_content.count(normalized_old)
                if occurrences > 1:
                    message = (
                        f"Found {occurrences} occurrences of the text in {path}. "
                        f"The text must be unique. Please provide more surrounding "
                        f"context to make it unique, or use replace_all: true."
                    )
                    return ToolResult(success=False, message=message)
                new_content = (
                    normalized_content[:idx]
                    + normalized_new
                    + normalized_content[idx + len(normalized_old):]
                )

            if normalized_content == new_content:
                message = (
                    f"No changes made to {path}. "
                    f"The replacement produced identical content."
                )
                return ToolResult(success=False, message=message,render = f"{self.name} 执行失败")

            # Restore BOM and original line endings
            new_content = bom + _restore_line_endings(new_content, original_ending)

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        async with aiofiles.open(absolute_path, 'w', encoding='utf-8') as f:
            await f.write(new_content)

        diff = _generate_diff(raw_content, new_content)
        return ToolResult(
            success=True,
            data=f"Successfully edited {path}.\n\nDiff:\n{diff}" if diff.strip() else f"Successfully edited {path}.",
            render=f"{self.name} 修改完成 {absolute_path.name}  "
        )


class ListDirectoryTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_directory"

    @property
    def label(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. Shows files and subdirectories "
            "with type indicators ([DIR] / [FILE]) and human-readable file sizes.\n\n"
            "## When to use\n"
            "- Exploring project structure before reading specific files — always prefer this "
            "over blindly guessing file paths\n"
            "- Finding files matching a pattern (use glob parameter, e.g. '*.py', '*.csv')\n"
            "- Assessing data file sizes without opening them\n\n"
            "## Parameters\n"
            "- depth: recursion depth (default 1 = current dir only, 2 = one level of subdirs, etc.). "
            "Use depth > 1 to explore nested project structure without multiple calls\n"
            "- glob: filter entries by name pattern (e.g. '*.py', 'test_*.py', '*.csv'). "
            "Standard shell wildcards, not regex"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Can be relative to project root or absolute."
                },
                "depth": {
                    "type": "integer",
                    "description": "Recursion depth (default 1 = current dir only). Set to 2+ to explore subdirectories."
                },
                "glob": {
                    "type": "string",
                    "description": "Filter entries by shell wildcard pattern (e.g. '*.py', 'test_*.py'). Not regex."
                },
            },
            "required": ["path"]
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
            log.warning("list_directory 执行异常: {}", exc)
            return ToolResult(success=False, message=f"列出目录失败: {exc}",render=f"{self.name} 执行失败")

    async def _do_execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path = params["path"]
        depth = params.get("depth", 1)
        glob_filter = params.get("glob")

        sess = await ToolSession.load(params)
        absolute_path = resolve_read_path(path, sess.app_id, sess.user_id)

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if not absolute_path.exists():
            return ToolResult(
                success=False,
                message=f"目录不存在: {path}",
                render=f"{self.name} 路径解析失败：{path.split("/")[-1]}"
            )
        if not absolute_path.is_dir():
            return ToolResult(
                success=False,
                message=f"不是目录: {path}。请使用 read_file 工具读取文件内容。",
                render=f"{self.name} 路径解析失败：{path.split("/")[-1]}"
            )

        entries: list[str] = []
        dir_count = 0
        file_count = 0

        def _collect(current: Path, prefix: str, current_depth: int):
            nonlocal dir_count, file_count
            if current_depth > depth:
                return

            try:
                children = sorted(
                    current.iterdir(),
                    key=lambda x: (x.is_file(), x.name.lower())
                )
            except PermissionError:
                entries.append(f"{prefix}[DENIED] (permission error)")
                return

            for child in children:
                if signal and signal.is_set():
                    return

                # Apply glob filter (only to files, dirs always shown unless filtered)
                if glob_filter and child.is_file() and not fnmatch.fnmatch(child.name, glob_filter):
                    continue

                if child.is_dir():
                    dir_count += 1
                    indent = prefix.replace("├── ", "│   ").replace("└── ", "    ")
                    entries.append(f"{prefix}[DIR]  {child.name}/")
                    _collect(child, indent + ("├── " if current_depth < depth else ""), current_depth + 1)
                else:
                    file_count += 1
                    try:
                        size_str = format_size(child.stat().st_size)
                    except OSError:
                        size_str = "?"
                    entries.append(f"{prefix}[FILE] {child.name} ({size_str})")

            if signal and signal.is_set():
                raise asyncio.CancelledError("Operation aborted")

        _collect(absolute_path, "", 1)

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if not entries:
            return ToolResult(
                success=True,
                data=f"目录为空: {absolute_path}",
                # BUG-3 修复：原文案复制粘贴错误，把「目录为空」渲染成了「路径解析失败」
                render=f"{self.name} 目录为空：{absolute_path}"
            )

        header = f"Contents of {absolute_path} ({dir_count} dirs, {file_count} files)"
        if glob_filter:
            header += f" [glob: {glob_filter}]"
        if depth > 1:
            header += f" [depth: {depth}]"
        output = header + ":\n" + "\n".join(entries)
        return ToolResult(success=True, data=output,render=f"{self.name} 执行成功 {header}")


class DeleteFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "delete_file"

    @property
    def label(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "Delete a file or an empty directory within the workspace.\n\n"
            "## When to use\n"
            "- Cleaning up temporary/intermediate files after analysis is complete\n"
            "- Removing stale reports or charts before regenerating\n"
            "- Deleting empty directories that are no longer needed\n"
            "- NEVER use this to delete source code or configuration without explicit user instruction\n\n"
            "## Safety limits\n"
            "- Only files/directories within the workspace (project directory) can be deleted — "
            "external paths are rejected\n"
            "- Non-empty directories require explicit confirmation via recursive: true\n"
            "- After deletion, verify with list_directory that the file is gone"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file or directory to delete. Must be within the workspace."
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Required to be true when deleting a non-empty directory. Acts as a safety confirmation."
                },
            },
            "required": ["path"]
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
            log.warning("delete_file 执行异常: {}", exc)
            return ToolResult(success=False, message=f"删除文件失败: {exc}",render=f"{self.name} 删除失败")

    async def _do_execute(
            self,
            params: dict,
            signal: asyncio.Event | None = None,
    ) -> ToolResult:
        path = params["path"]
        recursive = params.get("recursive", False)

        sess = await ToolSession.load(params)
        absolute_path = resolve_read_path(path, sess.app_id, sess.user_id)

        # Enforce workspace boundary: reject paths outside the project workspace
        workspace_root = sess.workspace().resolve()
        try:
            absolute_path.resolve().relative_to(workspace_root)
        except ValueError:
            return ToolResult(
                success=False,
                message=f"安全限制: 只能删除工作区内的文件。路径超出工作区范围: {path}",
                render=f"安全限制: 只能删除工作区内的文件。路径超出工作区范围: {path}"
            )

        if not absolute_path.exists():
            return ToolResult(
                success=False,
                message=f"文件或目录不存在: {path}",
                render=f"{self.name} 文件或目录不存在 {path.split('/')[-1]} "
            )

        if signal and signal.is_set():
            raise asyncio.CancelledError("Operation aborted")

        if absolute_path.is_dir():
            # Check if directory has contents
            try:
                has_contents = any(True for _ in absolute_path.iterdir())
            except PermissionError:
                return ToolResult(
                    success=False,
                    message=f"没有权限访问目录: {path}",
                    render=f"{self.name} 没有权限访问目录 {path.split('/')[-1]}"
                )

            if has_contents and not recursive:
                return ToolResult(
                    success=False,
                    message=(
                        f"目录不为空: {path}。"
                        f"请使用 list_directory 确认目录内容后，"
                        f"如需删除整个目录请设置 recursive: true 作为安全确认。"
                    ),
                    render = f"{self.name} 准备删除整个目录 {path.split("/")[-1]} 需要再次确认"
                )

            if has_contents:
                shutil.rmtree(str(absolute_path))
                return ToolResult(
                    success=True,
                    data=f"已递归删除目录: {path}",
                    render=f"{self.name} 已递归删除目录: {path.split("/")[-1]}"
                )
            else:
                absolute_path.rmdir()
                return ToolResult(
                    success=True,
                    data=f"已删除空目录: {path}",
                    render=f"{self.name} 已删除空目录： {path.split('/')[-1]}"
                )
        else:
            absolute_path.unlink()
            return ToolResult(
                success=True,
                data=f"已删除文件: {path}",
                render=f"{self.name} 已删除文件: {path.split('/')[-1]}"
            )