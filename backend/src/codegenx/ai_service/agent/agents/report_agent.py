"""报告子 Agent（P0-4 重构）：图表规划 → 逐张出图 → 拼接 Markdown 报告回传 lead。

职责划分：
- 图表规划（哪些图/什么维度/怎么排版）与结论撰写是 LLM 决策，由本 agent 承担；
- 基础绘图是确定性能力，收口为 plot_chart 工具（本 agent 逐张调用）；
- 报告落盘与前端可渲染结构由 generate_report 工具承担；
- 最终答复回传 lead：报告路径 + 图表清单 + 核心结论（lead 汇总作答用）。

执行循环由 BaseReActAgent 原生 tool_calls 循环承担：think → plot_chart × N →
generate_report → 自由文本作答。
"""
from __future__ import annotations

from typing import Any

from codegenx.ai_service.agent.action import ActOutput
from codegenx.ai_service.agent.agents.agent import AgentMessage
from codegenx.ai_service.agent.agents.base_react_agent import BaseReActAgent
from codegenx.ai_service.context.common_context import CommonContext
from codegenx.ai_service.prompt.prompt_manager import PromptConfig
from codegenx.ai_service.schema.ai_schema import UserAIRequest
from shared import log


class ReportAgent(BaseReActAgent):
    """报告生成智能体（规划图表 + 逐张出图 + 拼接 Markdown 报告回传）。"""

    name = "reporter"

    profile: PromptConfig = PromptConfig(
        role="Report Writer",
        name="reporter",  # 和 config.json 中的 name 对应
        goal="按任务要求规划图表并逐张生成，拼接成图文并茂的 Markdown 分析报告，给出结论",
        desc="规划需要哪些图表（维度/指标/标题），调用 plot_chart 逐张出图，"
             "生成 Markdown 报告并回传报告路径与核心结论",
        is_retry_chat=False,
        retry_goal="",
        retry_constraints=[],
        constraints=[
            "工作流程（严格按序）：①规划：根据任务与已有数据确定图表清单（每张图展示什么维度/指标、"
            "用什么图型、标题怎么写）；②出图：调用 plot_chart 一张张生成（bar/line/pie/scatter，"
            "categories 与 series.values 必须等长；pie 只取第一组系列）；"
            "③汇总：调用 generate_report 把文字、数据表、图表引用（![标题](charts/xx.png)）"
            "与结论拼接成 Markdown 报告落盘。",
            "出图工具选择：常规图型（柱/折线/饼/散点）一律用 plot_chart 并传 save_image=true"
            "（对话内展示交互式 ECharts 图，同时落 PNG 供报告引用）；只有 plot_chart 表达不了的"
            "复杂图（组合图/双轴/桑基/热力/自定义标注）才用 run_python 写代码出图。",
            "图表数量按任务需要决定（通常 1-3 张），不为凑数出图；每张图必须有明确的分析目的。",
            "若任务中数据不足以支撑某张图，直接说明缺什么口径，禁止编造数据。",
            "报告结构：# 标题 → 结论摘要 → 数据表 → 图表（带引用）→ 分点结论与依据。",
            "最终答复回传给主智能体的内容必须包含：报告文件路径、图表文件清单、核心结论（2-4 条）；"
            "不要复述整篇报告正文。",
            "工具失败时如实回传失败原因与已完成的部分（已生成的图表清单），不要假装成功。",
        ],
        resource_prompt="",
        expand_prompt="",
        examples="",
        out_schema="最终答复格式：报告路径 + 图表清单 + 核心结论。不要输出 JSON。",
    )

    def __init__(self, context: CommonContext | None = None):
        super().__init__(context=context)

    async def observation(self, msg: AgentMessage, act_output: ActOutput,
                          llm_response: dict[Any, Any]) -> AgentMessage:
        """观察最终答复：报告路径+图表清单+结论回传调用方（lead 汇总消费）。"""
        final_text = str(llm_response.get("content") or "").strip()
        if not final_text:
            msg.success = False
            return msg
        target = msg.sender or "user"
        log.info("ReportAgent 完成 | 返回 {} | 答复长度={}", target, len(final_text))
        return self._reply(msg, target, final_text)
