"""分析报告工具（P0-4）：Markdown 报告落盘系统产物目录 agent_output/reports/ 并返回前端可渲染结构。

报告正文由 LLM 生成，工具只负责落盘与展示元数据组装：
- data 给 LLM 的回执极简（路径 + 字数），避免整篇报告重复占用上下文；
- render / extras 承载展示用元数据（报告路径/标题/正文），由 SSE 透传前端渲染，
  不进 LLM 上下文；
- 未纳入 HITL 写操作名单（写操作仅限 write_file/edit_file/delete_file 等通用写）：
  报告固定落在系统产物目录下，风险可控，免审批避免打断分析流。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult, ToolSession
from shared import log

# 报告正文最大字符数（防单次调用撑爆落盘/前端渲染）
MAX_REPORT_CHARS = 60000


class GenerateReportTool(BaseTool):
    """生成并落盘 Markdown 分析报告（P0-4 报告工具）。"""

    @property
    def name(self) -> str:
        return "generate_report"

    @property
    def label(self) -> str:
        return "report"

    @property
    def description(self) -> str:
        return (
            "将分析结论落盘为 Markdown 报告（保存到系统产物目录 agent_output/reports/）。"
            "报告应包含：数据表、图表引用（引用 plot_chart 生成的图片文件名）、"
            "结论与依据；生成后把报告路径告知用户。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "报告标题（同时用作文件名主体，自动清洗非法字符）",
                },
                "markdown": {
                    "type": "string",
                    "description": "完整 Markdown 报告正文（含数据表、图表引用、结论）",
                },
            },
            "required": ["title", "markdown"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
    ) -> ToolResult:
        title = str(params.get("title") or "").strip()
        markdown = str(params.get("markdown") or "").strip()
        if not title:
            return ToolResult(success=False, message="title 为空，请提供报告标题")
        if not markdown:
            return ToolResult(success=False, message="markdown 为空，请提供报告正文")
        if len(markdown) > MAX_REPORT_CHARS:
            return ToolResult(
                success=False,
                message=f"报告正文过长（{len(markdown)} 字符 > {MAX_REPORT_CHARS}），请精简后重试",
            )

        # 解析会话工作区：报告固定落系统产物目录 reports/
        sess = await ToolSession.load(params)
        report_dir = (sess.output_dir() / "reports").resolve()
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(success=False, message=f"创建报告目录失败: {exc}")

        # 文件名：标题清洗非法字符 + 时间戳防覆盖
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:60] or "report"
        file_path = report_dir / f"{safe_title}_{time.strftime('%Y%m%d_%H%M%S')}.md"
        try:
            file_path.write_text(f"# {title}\n\n{markdown}\n", encoding="utf-8")
        except OSError as exc:
            return ToolResult(success=False, message=f"报告落盘失败: {exc}")

        log.info("GenerateReportTool 报告已落盘 | path={} | chars={}", file_path, len(markdown))
        return ToolResult(
            success=True,
            data=(f"报告已保存：{file_path.name}（{len(markdown)} 字符，位于系统产物目录 reports/）。"
                  "请把报告路径告知用户。"),
            render=f"分析报告：{title}",
            # 展示元数据：前端经 SSE extras 渲染，不进 LLM 上下文
            extras={
                "report": {
                    "title": title,
                    "path": str(file_path),
                    "file_name": file_path.name,
                    "markdown": markdown,
                    "created_at": time.time(),
                }
            },
        )
