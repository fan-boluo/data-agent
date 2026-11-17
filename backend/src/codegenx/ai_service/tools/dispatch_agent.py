"""lead 派发工具（agent-as-tool 同步执行协议）。

dispatch_agent 是一个普通工具：调用时同步执行目标子智能体的完整 run()，
其最终结果作为 tool result 回填对话，lead 在同一 ReAct 工具循环内继续决策。
同轮多次调用 = 并行执行（ToolExecutor.dispatch_tool_calls 经 gather +
max_parallel 信号量限流）。

派发不登记看板（无隐式副作用）；计划链路握手在工具内处理（命中看板就绪
计划任务时 claim + task_id 沿用、回传后闭环解锁下游）。

统一工具链路：本工具经 ToolExecutor.execute 执行，execute 签名声明
session_state 参数即自动注入完整会话状态（与 signal 注入同机制），
无需 lead agent 本地拦截。
"""

import asyncio
import time
from typing import Any

from codegenx.ai_service.agent.agents.agent import AgentMessage
from codegenx.ai_service.agent.agents.supervisor_board import TaskBoardCoordinator
from codegenx.ai_service.agent.runtime_schema import SubAgentInfo, raise_if_stop_signal
from codegenx.ai_service.context.sub_context import SubContext
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.tools.base import BaseTool, ToolResult
from shared import log

# 派发发起者：dispatch_agent 为 lead 专用编排工具（config tools 白名单控制可见性）
# （须与 config.json agents[].name / LeaderAgent.name 保持一致）
LEADER_AGENT_NAME = "leader"


class DispatchAgentTool(BaseTool):
    """派发子任务给指定智能体执行（lead 编排工具，agent-as-tool 同步执行）。"""

    @property
    def name(self) -> str:
        return "dispatch_agent"

    @property
    def label(self) -> str:
        return "orchestration"

    @property
    def description(self) -> str:
        return (
            "派发一个子任务给指定智能体执行。调用后子智能体立即执行，其最终结果作为"
            "工具结果返回，你基于结果继续决策。同一回复中多次调用 = 相互独立的子任务并行"
            "执行。派发任务看板中已登记的就绪任务时 task 填看板任务原文描述（系统自动"
            "复用看板登记）。禁止派发给 leader 或 user。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "目标智能体 name（执行智能体或 planner；禁止 leader / user）",
                },
                "task": {
                    "type": "string",
                    "description": "完全自包含的任务描述：执行智能体只看得到 task 文本，"
                                   "执行所需的全部信息（文件名、数值、统计口径）必须直接写进 task；"
                                   "派发 planner 时填用户原始请求",
                },
            },
            "required": ["agent", "task"],
        }

    async def execute(
        self, params: dict, signal: asyncio.Event | None = None,
        session_state: Any | None = None,
    ) -> ToolResult:
        """同步执行一次派发：实例化目标子智能体 run()，最终结果作为工具结果返回。

        - 参数校验/非法目标/防重复规划以失败结果回给 LLM 自纠；
        - 计划链路握手：task 命中看板就绪计划任务 → claim（in_progress+执行者）、
          task_id 沿用；回传后 consume 闭环（completed，解锁下游 depends_on）；
        - 子智能体结果写入会话历史真相源（add_agent_result，跨轮追问可见）；
        - 停止信号传播（TurnStoppedError）终止整个请求。
        """
        agent = str(params.get("agent") or "").strip()
        task_text = str(params.get("task") or "").strip()
        # 参数与目标校验（错误说明回给 LLM 自纠）
        if not agent:
            return ToolResult(success=False, message="缺少 agent 参数：请填写目标智能体 name")
        if agent in (LEADER_AGENT_NAME, "supervisor", "user"):
            return ToolResult(success=False, message=f"不能派发给 {agent}：执行与汇总由你直接完成")
        if not task_text:
            return ToolResult(success=False, message="缺少 task 参数：任务描述必须完全自包含")
        if session_state is None:
            return ToolResult(success=False, message="会话上下文缺失，无法派发")

        spec = get_app().agents.get(agent)
        if spec is None or spec.cls is None:
            return ToolResult(success=False, message=f"未注册的智能体：{agent}")

        # 全局可用性拦截：管理员停用 / 模型链全部熔断打开的目标不可派发（回给 LLM 改路）
        from codegenx.resource_service.agents import get_unavailable_agent_names

        unavailable_reason = (await get_unavailable_agent_names()).get(spec.name)
        if unavailable_reason:
            return ToolResult(
                success=False,
                message=f"智能体 {agent} 当前不可用（{unavailable_reason}），请改派其他可用智能体或自行处理",
                error_class="agent_unavailable",
            )

        # 停止信号：派发执行前置检查（子智能体 run 内部亦逐轮检查）
        raise_if_stop_signal(session_state)

        board = TaskBoardCoordinator()
        # 防重复规划守卫：本请求看板已有计划任务时拒派 planner
        if agent == "planner" and board.has_planner_tasks(session_state.request):
            return ToolResult(
                success=False,
                message="本请求看板已有计划任务：请直接执行或派发看板就绪任务，若需重新规划，先删除旧看板计划，勿重复规划",
            )

        # 检查要执行的task是不是在看板中的，如果是则认领更新看板
        # 计划任务复用：命中就绪计划待办 → claim认领 后 task_id 沿用（闭环握手解锁下游）
        reuse = board.pick_ready_planner_task(session_state.request, task_text)
        task_id = None
        step_id = None
        if reuse is not None:
            board.claim_board_task(session_state.request, reuse.id, agent)
            task_id = reuse.id
            step_id = reuse.step_id

        # 生命周期登记：activate_turn.active_sub_agents 观测注册表
        sub_key = f"{agent}:{task_id or task_text[:16]}"
        sub_info = SubAgentInfo(agent_name=agent, task_id=str(task_id or ""), started_at=time.time())
        session_state.activate_turn.active_sub_agents[sub_key] = sub_info

        # 同步执行一次（失败不自动外重试）：
        # 工具/LLM 层的瞬态/超时/熔断已在调用链路内重试；子智能体自身 ReAct 循环内
        # 已基于工具失败结果做 observation 阶段的 in-loop 改路/重试。
        # 这里只把最终失败原因作为 tool result 回填，由 leader 决定换派/改路。
        sub_context = SubContext(
            session_id=session_state.session_id,
            app_id=session_state.app_id,
            user_id=session_state.user_id,
            db_name=session_state.db_name,
            agent_name=agent,
        )
        dispatch = AgentMessage(
            sender=LEADER_AGENT_NAME,
            recipient=agent,
            task=task_text,
            task_id=task_id,
            step_id=step_id,
            request_id=str(getattr(session_state, "request_id", "") or ""),
            session_id=str(getattr(session_state, "session_id", "") or ""),
            success=True,
        )
        log.info("【lead DISPATCH】{} ──► {} | task_id={} | task={}",
                 LEADER_AGENT_NAME, agent, task_id, task_text[:80])
        # context 由 __init__ 绑定，run 不再传 context 参数
        child_msg = await spec.cls(context=sub_context).run(dispatch, session_state, is_stream=True)
        sub_info.finished_at = time.time()
        sub_info.success = bool(child_msg.success) if child_msg is not None else False
        sub_info.message = str(child_msg.result or "")[:500] if child_msg is not None else ""

        # 看板闭环（计划任务）：completed + 解锁下游
        if child_msg is not None and child_msg.task_id is not None:
            board.complete_task(task_id=child_msg.task_id, executor=child_msg.sender, request=session_state.request)

        # 工具结果本来就会加入上下文，这里不用重复加
        result_text = str(child_msg.result or "").strip() if child_msg is not None else ""
        success = bool(child_msg.success) if child_msg is not None else False

        # 失败/空结果以失败说明回填（LLM 自纠：改口径重派或如实汇总）
        # 归因：非基础设施故障，不计工具熔断
        if child_msg is None:
            return ToolResult(success=False, message="执行失败：子智能体未返回结果",
                              error_class="subagent_failed")
        if success:
            return ToolResult(success=True, data=result_text or "执行完成，但子智能体未返回结果文本。")
        # 子智能体失败：失败原因原文回填（不重复归因）；error_class 仅用于指标口径，
        # 不计入 dispatch_agent 自身熔断（非基础设施故障）
        return ToolResult(success=False, message=f"执行失败：{result_text or '（无失败说明）'}",
                          error_class="subagent_failed")

