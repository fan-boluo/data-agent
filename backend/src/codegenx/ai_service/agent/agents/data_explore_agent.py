from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from codegenx.ai_service.agent.action import ActOutput
from codegenx.ai_service.agent.agents.agent import AgentMessage
from codegenx.ai_service.agent.agents.base_react_agent import BaseReActAgent
from codegenx.ai_service.context.common_context import CommonContext
from codegenx.ai_service.prompt.prompt_manager import PromptConfig
from codegenx.ai_service.schema.ai_schema import UserAIRequest
from codegenx.ai_service.semantic.retrieval import hybrid
from shared import log, settings


class DateExploreAgent(BaseReActAgent):
    """数据探索（工具化 agent：自主调用 execute_postgresql 完成查询并作答）

    执行循环由 BaseReActAgent 的原生 tool_calls 循环承担：
    think →（LLM 发起 execute_postgresql）→ 工具结果回填 → 继续 think → … → 无工具调用时自由文本作答。
    简单查询一轮即可自总结答复，复杂任务多轮迭代。
    """

    name = "data_explore"

    profile: PromptConfig = PromptConfig(
        role="Data Analyst",
        name="data_explore",  # 和config里面的对应
        goal="使用 execute_postgresql 工具查询 {{type}} 数据库，基于真实查询结果回答任务并给出结论",
        desc="根据数据库资源进行sql数据分析",
        is_retry_chat=False,
        retry_goal="",
        retry_constraints=[],
        constraints=[
            # ── 语义层优先原则（强制，禁止 information_schema 自探） ──
            "表名、字段名、数据字典一律从**语义层工具**获取：",
            "  ① 不确定有哪些表 → search_tables（按问题关键词粗召回）；",
            "  ② 已知表名想看列/类型/主键/注释 → describe_table_card（精读）；",
            "  ③ 多表 JOIN 前**必须** find_join_path 确认连接键，禁止自行猜测；",
            "  ④ 业务口径不明 → search_metrics（指标名/同义词/口径/表达式）。",
            "**禁止**把 SELECT ... FROM information_schema.tables/columns 作为首选手段——",
            "这属于绕过语义层的自探，每次自探都会引入 schema 名称猜测（business/dvdrental 等）和 LLM 额外推理开销。",
            "只有在语义层工具返回失败或未召回任何结果时，才允许 information_schema 作为兜底手段。",
            "SQL 编写必须严格基于语义层返回的真实表名、列名、类型，禁止编造不存在的字段。",
            "跨表关联时严格遵循 find_join_path 返回的连接路径，不要用自行猜测的外键关系。",
            "仅允许只读语句（SELECT/SHOW/DESC），禁止 INSERT/UPDATE/DELETE/DDL 等写操作。",
            "禁止自行编造数据作为查询条件，仅能使用任务中提供的数据值作为查询条件。",
            "查询结果行数较多时合理使用聚合或 LIMIT；一次查询不够时可分步多次查询。",
            "查询完成后必须基于工具返回的真实数据给出结论：直接回答任务问题，简明陈述关键数值与依据，禁止编造数值。",
            "若 SQL 执行报错，根据工具返回的错误信息修正后重试；多次失败则如实说明原因与已排除的方向。",
        ],
        resource_prompt="你可以查询的database及数据表schema如下:{{database_info}}",
        expand_prompt="",
        examples="",
        out_schema="最终答复直接用自然语言给出：先给结论，再列关键数据依据。不要输出 JSON，不要描述你的执行过程。",
    )

    def __init__(self, context: CommonContext | None = None):
        super().__init__(context=context)

    async def _prepare_prompt_params(self,request: UserAIRequest) -> dict:
        """把语义层召回的候选表卡注入 prompt（只聚焦用户当前选择的数据集）。

        current_db 格式为 "datasource.schema"（如 business.dvdrental）；
        方言固定取 settings.public_db_dialect。
        """
        current = str(getattr(request, "current_db", None) or "")
        public_ds = current.split(".")[0] if current else None
        return {
            "type": settings.public_db_dialect,
            "database_info": await hybrid.build_context(
                request.message, datasource=public_ds,
            ),
        }

    async def _prepare_agent(self, request: UserAIRequest):
        """ 准备agent所需的记忆，工具，skill，prompt 上下文等 """
        # db 元信息查询是 async，须在 super() 之前完成
        await super()._prepare_agent(request)

    async def observation(self, msg: AgentMessage, act_output: ActOutput, llm_response: dict[Any, Any]) -> AgentMessage:
        """观察最终答复：工具化循环结束后 LLM 的自由文本即结论，直接回传调用方。

        工具化模式下 act_output 只是 LLM 响应的封装（无 Action 链）；
        SQL 执行失败已通过 tool 消息回填由 LLM 自行修正重试，走到这里的
        都是已生成最终答复的正常路径。任务自包含由 Supervisor 派发保证，
        本 agent 只对自身任务负责，不感知总体目标。
        """
        final_text = str(llm_response.get("content") or "").strip()
        if not final_text:
            # 防御：run 循环已兜底空回复，正常不会走到这里
            msg.success = False
            return msg

        # 回传调用方（lead 同步派发执行器消费 result；task_id 原样保留供计划链路闭环）
        target = msg.sender or "user"
        log.info("DataExploreAgent 完成 | 返回 {} | 答复长度={}", target, len(final_text))
        # 结论写进 result（回传跳 result 承载结果文本，task 保留原任务便于追溯）
        return self._reply(msg, target, final_text)



if __name__ == "__main__":
    from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState

    def _make_session_state() -> RuntimeSessionState:
        """构造一个最小可用的 RuntimeSessionState（只需 session_id + request）。"""
        from codegenx.ai_service.schema.ai_schema import UserAIRequest

        request = UserAIRequest(
            appId="test-app",
            userId="test-user",
            message="测试消息",
            sessionId="test-session",
            traceId="t1",
            requestId="r1",
        )
        return RuntimeSessionState(session_id="test-session", request=request)

    async def test():
        agent = DateExploreAgent()
        agent.resolve_agent_config()
        await agent._prepare_agent(session_state=_make_session_state())

    asyncio.run(test())
