"""TaskBoardCoordinator —— 看板协调器
集中全部看板确定性操作（无 LLM 参与）。agent-as-tool 同步派发后看板语义收窄
为「计划任务看板」：登记与闭环只服务计划链路（planner 登记的 depends_on DAG
解锁握手）；普通派发不再登记看板（无隐式副作用），LLM 经 task_create/update/
get/list 工具显式管理（如亲自执行的计划任务完成后标记 completed）。

- 计划登记：register_plan（planner 产出计划后自行落板——计划只拆任务不指派
  执行者，怎么执行由 lead agent 逐任务决定）
- 计划任务认领：pick_ready_planner_task / claim_board_task（lead 派发时复用
  就绪计划任务，task_id 沿用）
- 结果闭环：consume_child_result（task_id 严格匹配，completed 解锁下游）
- 防重复规划：has_planner_tasks
- prompt 渲染：render_board（每次 think 前刷新，无需 turn 重置钩子）

编排上下文 OrchestrationState 已删：看板（按 request 落盘）即唯一共享状态，
执行轨迹观测由后续 trace/hook 模块承担。

看板复用全局 TaskBoardService（.data/{user}/{app}/session/{sid}/.tasks/{request_id}.json，
按 request 单文件隔离，task_id 各自从 1 计数）。
"""

from codegenx.ai_service.agent.agents.agent import AgentMessage
from codegenx.ai_service.schema.ai_schema import UserAIRequest
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.task.task_manager import TaskRecord, TaskStatus
from shared import log


class TaskBoardCoordinator:
    """看板协调器：计划任务看板读写（lead 编排的确定性动作层）。"""

    # ── 服务与上下文访问 ──

    @staticmethod
    def _service():
        return get_app().tasks

    @staticmethod
    def _ids(request) -> dict:
        # request_id 一起下传：任务看板按 request 单文件隔离，task_id 为 request 内局部 id
        return {
            "user_id": request.user_id,
            "app_id": str(request.app_id or ""),
            "session_id": request.session_id,
            "request_id": str(getattr(request, "request_id", "") or ""),
        }

    # ── 计划登记（planner 侧调用）──

    def register_plan(self, request:UserAIRequest, plan_dict: dict) -> dict | None:
        """Planner 计划落板（planner observation 调用）。

        计划只做任务分解、不指派执行者（executor_agent 留空，由 lead agent
        逐任务决定自己执行还是派发哪个子智能体）：
        - 逐 step 登记看板任务（subject / depends_on=DAG 前置 / source=planner）；
        - 返回登记摘要 {goal, total, ready} 供 planner 回传
          （lead agent 据摘要与看板视图审查并自主决定执行方式）。
        """
        tasks = self._service()
        if tasks is None or not (isinstance(plan_dict, dict) and plan_dict.get("steps")):
            return None
        ids = self._ids(request)
        step_task_map: dict = {}   # step_id → task_id 依赖换算
        record_ids: list = []
        for s in sorted(plan_dict.get("steps", []), key=lambda x: x.get("step_id", 0)):
            dep_ids = [step_task_map[d] for d in (s.get("depends_on") or []) if d in step_task_map]
            record = tasks.create(
                subject=str(s.get("task", "") or ""),
                description="; ".join(str(r) for r in (s.get("resources") or [])),
                depends_on=dep_ids,
                step_id=int(s.get("step_id", 0) or 0) or None,
                source="planner",
                **ids,
            )
            step_task_map[s.get("step_id")] = record.id
            record_ids.append(record.id)
        ready = sum(1 for t in tasks.list_all(**ids) if t.id in record_ids and tasks.is_ready(t))
        log.info("Planner: 计划已登记任务看板 | goal={} | tasks={} | ready={}",
                 str(plan_dict.get("goal", ""))[:80], len(record_ids), ready)
        return {
            "goal": str(plan_dict.get("goal", "")),
            "total": len(record_ids),
            "ready": ready,
        }

    def has_planner_tasks(self, request) -> bool:
        """本 request 看板是否已有 planner 来源任务（防重复规划导致重复登记）。"""
        tasks = self._service()
        if tasks is None:
            return False
        return any(
            t.source == "planner" for t in tasks.list_all(**self._ids(request))
        )

    # ── 计划任务认领（lead 派发时复用） ──

    def pick_ready_planner_task(self, request, subject: str) -> TaskRecord | None:
        """查找相同 subject 的「pending 就绪」计划任务（source=planner）
        → 供 dispatch_agent 复用派发（不新建重复任务）。

        计划任务无预指派执行者（执行者由本次派发的 agent 参数决定）。
        """
        tasks = self._service()
        if tasks is None:
            return None
        target = self._norm_subject(subject)
        if not target:
            return None
        for t in tasks.list_all(**self._ids(request)):
            if (t.source == "planner" and t.status is TaskStatus.PENDING
                    and self._norm_subject(t.subject) == target and tasks.is_ready(t)):
                return t
        return None

    def claim_board_task(self, request:UserAIRequest, task_id: int, agent: str) -> None:
        """计划任务认领：写入本次派发的执行者并标记 in_progress
        （task_id 沿用看板任务，回传后 consume_child_result 闭环解锁下游）。"""
        tasks = self._service()
        if tasks is not None:
            tasks.update(task_id, status="in_progress", executor_agent=agent,
                         **self._ids(request))
        log.info("lead: 认领看板计划任务 #{} → {}（in_progress）", task_id, agent)

    @staticmethod
    def _norm_subject(text: str) -> str:
        """任务描述归一化：去除全部空白字符后比较（仅空白差异视为相同任务）。"""
        return "".join(str(text or "").split())

    # ── 结果闭环 ──

    def complete_task(self, task_id: str,executor:str, request:UserAIRequest) -> None:
        """子智能体回传 → 看板计划任务标记 completed（自动解锁下游 depends_on）。

        task_id 严格匹配（计划链路派发时 claim 的任务）：普通派发不登记看板、
        不携带 task_id，直接跳过（无隐式副作用）。
        """
        if task_id is None:
            return
        tasks = self._service()
        if tasks is None:
            return
        ids = self._ids(request)
        t = tasks.get(task_id, **ids)
        if t is None or t.status != TaskStatus.IN_PROGRESS:
            log.warning("lead: 回传 task_id={} 无对应 in_progress 看板任务，跳过闭环 | executor={}",
                        task_id, executor)
            return
        tasks.complete(t.id, **ids)
        log.info("lead: 看板计划任务 #{} 已完成, execuotr： {}", t.id, executor)

    # ── prompt 渲染 ──

    def render_board(self, session_state) -> str:
        """渲染任务看板文本：读 TurnPrompts.task_prompt（单一来源）。

        每次 think 前刷新：turn 过程中任务在完成、依赖在解锁，需最新就绪集
        （planner 登记的计划任务同样经此呈现，lead agent 据此自主决定执行方式）。
        """
        cm = getattr(session_state, "context", None)
        if cm is None:
            return "（暂无任务看板）"
        cm.refresh_task_prompt(str(getattr(session_state, "request_id", "") or ""))
        return cm.prompts.task_prompt or "（暂无任务看板）"

