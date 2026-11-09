"""
skill　暂时没有加，这个工具就先不注册了　取消继承　ＢａｓｅＴｏｏｌ
"""
import asyncio
from typing import Any

from codegenx.ai_service.tools.base import BaseTool, ToolResult
from shared import log


class LoadSkillTool:
    @property
    def name(self) -> str:
        return "load_skill"

    @property
    def label(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return "Load the full body of a named skill into the current context. Use after the skill catalog indicates a relevant skill."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the available skill catalog."}
            },
            "required": ["name"],
        }

    async def execute(self, params: dict, signal: asyncio.Event | None = None) -> ToolResult:
        name = str(params.get("name", "")).strip()
        try:
            # P2 服务化：经全局容器取 SkillRegistry（原 SkillLoader 临时实例已废弃）
            from codegenx.ai_service.system_app import get_app

            skill_text = get_app().skill_manager.full_text(name)
            return ToolResult(
                success=True,
                data=skill_text,
                render=f"加载技能: {name}",
            )
        except Exception as exc:
            log.error(f"Failed to load skill {name}: {exc}")
            return ToolResult(success=False, message=f"Error loading skill: {exc}", render=f"加载技能失败: {name}")