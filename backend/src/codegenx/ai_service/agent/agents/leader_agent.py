"""
LeaderAgent —— lead agent：统一工具面编排与执行（agent-as-tool，同步派发）

参照主流 agent-as-tool 设计（Anthropic Task 工具 / OpenAI Agents SDK as_tool）：
dispatch_agent 是一个普通工具——调用时同步执行目标子智能体的完整 run()，
其最终结果作为 tool result 回填对话，lead 在同一 ReAct 工具循环内继续决策。
派发不登记看板、不产生跨 agent 消息跳（调度循环只剩 user → lead → user）。


- 子智能体结果直接以 tool 消息进对话上下文——审查即 ReAct 本身
  （看到结果继续思考），无需审查框架/历史注入改写/批信封；
- 子智能体结果同步写入会话历史真相源（add_agent_result），跨轮追问可见；
- 计划链路握手在派发工具内处理：派发任务匹配看板就绪计划任务时复用
  task_id（claim in_progress → 回传 consume 闭环，解锁下游 depends_on DAG）；
- 看板由 LLM 经 task_create/update/get/list 显式管理（亲自执行的计划任务
  完成后 task_update 标记 completed 解锁下游），派发无隐式看板副作用。

职责分层（自原单类实现拆分）：
- LeaderAgent（本文件）：编排流程 + 输入组装 + 终答（会话历史直写 cm 真相源）
- TaskBoardCoordinator（supervisor_board.py)：看板的全部确定性操作

编排状态 OrchestrationState 已退役：看板（按 request 落盘）即唯一共享状态，
执行轨迹观测由后续 trace/hook 模块承担。
"""

from typing import Any

from codegenx.ai_service.agent.action import ActOutput
from codegenx.ai_service.agent.agents.agent import AgentMessage
from codegenx.ai_service.agent.agents.base_react_agent import BaseReActAgent
from codegenx.ai_service.context.common_context import CommonContext
from codegenx.ai_service.prompt.prompt_manager import PromptConfig
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.schema.ai_schema import UserAIRequest
from shared.constants import get_code_dir
from shared.utils.log_util import log


class LeaderAgent(BaseReActAgent):
    """lead agent：① 直答 / ② 自干 / ③ 派发子智能体（含 planner），统一工具面。"""
    # name 必须与 config.json agents[].name 一致（resolve_agent_config 按此匹配配置：
    # 模型链、max_steps、tools 白名单）；注册规格名取 profile.name（"leader"）
    name = "leader"
    # 类属性：register() 经 cls.profile 构建注册规格
    #（actions 为空：决策走 tool_calls 分支，JSON Action 链已随工具化退役）
    profile: PromptConfig =PromptConfig(
        role="leader",
        name="leader",
        goal="你是数据分析系统的 lead agent（首席执行与编排智能体）：既能直接调用工具完成操作，"
             "也能派发子智能体并行处理任务，还能把复杂请求交给 planner 拆解为分步计划。"
             "根据用户请求与执行结果自主判断推进方式，多轮推进直至给出最终答复。",
        desc="lead agent：直接执行任务，编排派发子智能体，复杂任务先规划",
        is_retry_chat=False,
        retry_goal="",
        retry_constraints=[],
        constraints=[
            # ── 语义层优先原则（强制，禁止 information_schema 自探） ──
            "涉及数据库操作（自己执行或派发）时，表名/字段/数据字典一律从**语义层工具**获取：",
            "  ① 不确定有哪些表 → search_tables（按问题关键词粗召回）；",
            "  ② 已知表名想看列/类型/主键/注释 → describe_table_card（精读）；",
            "  ③ 多表 JOIN 前**必须** find_join_path 确认连接键，禁止自行猜测；",
            "  ④ 业务口径不明 → search_metrics（指标名/同义词/口径/表达式）。",
            "**禁止**把 SELECT ... FROM information_schema.tables/columns 作为首选手段——",
            "这属于绕过语义层的自探，每次自探都会引入 schema 名称猜测（business/dvdrental 等）和 LLM 额外推理开销。",
            "只有在语义层工具返回失败或未召回任何结果时，才允许 information_schema 作为兜底手段。",
            # ── 决策原则：整体从直接到间接，LLM 自主判断 ──
            "自主判断推进方式，整体原则：能用更直接的方式解决就不用更绕的方式——已有足够信息直接回答；自己调工具就能完成的操作自己做；需要并行处理或重计算的任务派发子智能体；任务复杂、步骤间存在依赖、需要先统筹拆解时派发 planner 制定计划。各智能体的职责见可用智能体清单。",
            "直接答复：打招呼、闲聊、无关/违规问题、信息歧义需反问，或已获取的信息足够回答用户时，不调用任何工具直接输出面向用户的文本。",
            "自己执行：查表结构、执行单条 SQL 统计、查看 CSV 清单与描述、简单 Python 计算等操作直接调用工具完成，工具结果回填后基于结果继续决策或汇总作答；派发子智能体的往返开销远大于这类操作，不要为小事派发。",
            "派发子智能体（dispatch_agent）：调用后子智能体立即执行，其最终结果作为工具结果回填对话，你基于结果继续决策。多个相互独立的子任务在同一回复中多次调用 = 并行执行。存在运行时数据依赖的后续任务等前置结果回填后再派发，派发时把已获得的前置结果内联进 task。",
            "派发 planner：任务复杂、需要先拆解为分步计划时，将用户原始请求派发给 planner（task 直接填用户原始请求，禁止附加编排建议；本请求看板已有计划任务时禁止再派发 planner）。planner 只产出任务分解并登记任务看板，每个任务怎么执行由你决定：自己用工具做或派发给合适的子智能体均可；你亲自执行的看板任务完成后调用 task_update 将其标记为 completed 以解锁下游任务。",
            # ── 职责分层与任务自包含 ──
            "对比、汇总、差值计算、结论生成是你的职责：禁止把跨数据源的汇总要求写进派发给执行智能体的 task 里。派发的 task 必须完全自包含：执行智能体只看得到 task 文本本身，看不到对话历史与其它任务，执行所需的全部信息（文件名、数值、统计口径）必须直接写进 task，禁止「结合前文」「上一步的结果」等引用；task 以执行智能体自身的能力与输出规范为准，禁止指定特定输出格式。",
            # ── 防循环约束 ──
            "回传结果出现矛盾时：最多派发一次口径明确的核实任务（一次性取全口径数据，描述必须与原任务不同），核实结果回填后必须直接汇总作答，禁止以格式不符等理由对同一智能体重复派发相同或相似任务。",
            "已派发过的任务不要重复派发：其执行结果已在对话中（工具结果），基于已有结果继续决策或汇总作答即可。",
        ],
        resource_prompt="可用资源如下：\n 有权限文件夹:{{available_file_path}}\n数据集:{{available_db}}",
        expand_prompt=(
            "可用的智能体如下：\n{{agents_list_desc}}\n\n"
        ),
        examples="",
        out_schema="",
        memory_prompt= "{{memory_prompt}}",
        taskbord_prompt= "{{board_desc}}",
        skill_prompt = "",
    )

    def __init__(self, context: CommonContext | None = None):
        super().__init__(context=context)

    # ── prompt 构建与输入组装 ──
    @staticmethod
    def agents_list_desc() -> str:
        """可用智能体清单（含 planner：dispatch_agent 可派发 planner 请求规划）。"""
        skip_roles = {"leader", "supervisor", "intent_recognition"}
        lines = []
        for spec in get_app().agents.all_specs():
            if spec.role in skip_roles:
                continue
            lines.append(f"{spec.name} - {spec.desc}")
        return "\n".join(lines) or "（暂无可用子智能体）"

    async def _prepare_agent(self, request: UserAIRequest) -> None:
        """预取记忆与摘要（expand 变量注入）后走基类组装；会话状态全程显式传参，实例不留 run 级字段。"""
        await super()._prepare_agent(request)

    @staticmethod
    def _db_hint(request: UserAIRequest) -> str:
        """数据集提示：明确当前数据集，未选择时引导反问，禁止跨库查询。"""
        current = str(getattr(request, "current_db", None) or "")
        available = list(getattr(request, "available_schemas", None) or [])
        if current:
            others = [s for s in available if s != current]
            hint = f"{current}（用户当前选择，本轮只在该数据集内查询，禁止跨库查询）"
            if others:
                hint += f"；其他可选数据集（仅当问题明显不匹配当前数据集时，提示用户切换）: {', '.join(others)}"
            return hint
        if available:
            return f"（用户未选择数据集；如需查询数据，必须先反问用户选择其中一个: {', '.join(available)}）"
        return "（无可用公共数据集）"

    async def _prepare_prompt_params(self,request) -> dict:
        """统一 prompt 变量：智能体清单（含 planner）+ 任务看板 + 记忆/摘要注入。"""

        # 以当前用户请求为 query 检索长期记忆；会话摘要由 compact_after_turn 后台产出
        query = str(getattr(request, "message", "") or "")
        app = get_app()
        _memory_prompt = await app.memory.load(
            query,
            user_id=request.user_id,
            app_id=request.app_id,
            session_id=request.session_id,
        )
        # 会话摘要为同步文件读取（SessionSummaryService.load 非 async），不能 await
        _session_summary = app.summary.load(
            user_id=request.user_id,
            app_id=request.app_id,
            session_id=request.session_id,
        )

        return {
            "available_file_path":get_code_dir(request.user_id,request.app_id),
            "available_db": self._db_hint(request),
            "agents_list_desc": self.agents_list_desc(),
            "board_desc": self._context.get_task_bord(),
            "memory_prompt": _memory_prompt or "（暂无相关记忆）",
        }

    # ── observation：终答跳（派发已在工具执行内完成，无派发跳组装） ──

    async def observation(self, msg: AgentMessage, act_output: ActOutput, llm_response: dict[Any, Any]) -> AgentMessage:
        """无工具调用且输出非空文本 = 终答：LLM 最终文本即面向用户的答复
        （direct_answer / finish 合一；子智能体结果已在对话中，汇总即终答）。"""
        final_text = str((llm_response or {}).get("content") or "").strip()
        msg.sender = self.agent_name
        msg.recipient = "user"
        msg.result = final_text or "任务已完成。"
        msg.task_id = None
        msg.step_id = None
        msg.success = True
        return msg
