from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, List
from codegenx.ai_service.agent.agent_schema import AgentState,AgentEventType
from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState, raise_if_stop_signal
from codegenx.ai_service.hook import HookAction, HookContext, HookEvent, hook_manager
from codegenx.ai_service.llm.async_client import get_llm
from codegenx.ai_service.llm.llm_recovery import invoke_stream_with_revovery
from codegenx.ai_service.llm.resilience import get_breaker, resilient_invoke
from codegenx.ai_service.schema.ai_schema import UserAIRequest
from codegenx.ai_service.utils.config import AgentConfig
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.trace import report_context_usage, start_span
from codegenx.ai_service.agent.tool_handler import get_tool_registry
from shared import log
from codegenx.ai_service.agent.agents.agent import Agent, AgentMessage
from codegenx.ai_service.context.common_context import CommonContext
from codegenx.ai_service.context.sub_context import SubContext
from codegenx.ai_service.utils.config import config
from codegenx.ai_service.agent.action import Action, ActOutput
from codegenx.ai_service.prompt.prompt_manager import PromptConfig


class BaseReActAgent(Agent):
    """
    一个基础思考，行动，验证agent
    """


    def __init__(
            self,
            context: CommonContext | None = None,
    ):
        Agent.__init__(self)
        # agent_config 已由 resolve_agent_config() 类方法解析为类属性；
        # 若尚未解析（如未走系统初始化），此处兜底调用。
        if self.agent_config is None:
            self.resolve_agent_config()

        self.max_tool_iterations = max(1, int(self.agent_config.max_tool_iterations or 40))
        self.max_same_tool_calls = self.agent_config.max_same_tool_calls or 3
        self.max_steps = self.agent_config.max_steps

        # 初始化即解析默认智能体模型链
        self.model_name = self.agent_config.resolved_model_name
        # 准备llm,breaker
        with suppress(Exception):
            get_llm(self.model_name)
            get_breaker(self.model_name)
        # agent 间寻址标识与收件箱（agent_name 全局唯一）
        self.agent_name = self.agent_config.name
        self._inbox: asyncio.Queue | None = None
        # 保留子类定义的 actions（类属性）；仅未设置时兜底为空链——
        # 无条件赋值会遮蔽类属性，导致 Action 链静默失效
        #（supervisor 已工具化：actions 为空，决策走 tool_calls 分支）
        if not getattr(self, "actions", None):
            self.actions: List[Action] = []
        # 保留子类定义的 profile；仅未设置时兜底为空 PromptConfig
        if not getattr(self, "profile", None):
            self.profile: PromptConfig = PromptConfig()
        # 上下文 init 绑定：leader 由入口传 SessionContext 真相源，
        # 子智能体由 dispatch_agent 传 SubContext；缺省兜底空 SubContext（独立调试）
        self._context = context or SubContext(agent_name=self.agent_name)
        log.info(f"初始化agent: {self.agent_name} 完成")


    # -----类方法：配置解析与注册---------------------

    @classmethod
    def resolve_agent_config(cls) -> None:
        """按 cls.name 匹配 config.agents，将配置存为类属性（系统初始化时调用）。

        匹配后同时填充 cls.id（来自配置）和 cls.agent_config。
        """
        matched = AgentConfig()
        for agent in config.agents or []:
            if (agent.name or "").strip() and agent.name.strip() == cls.name.strip():
                matched = agent
                break
        cls.agent_config = matched
        # 从配置同步 id 到类属性（配置优先，缺省回落 cls.name）
        cls.id = str(getattr(matched, "id", "") or cls.name)

    @classmethod
    def register(cls) -> None:
        """将自身规格注册到 AgentRegistry（系统初始化时调用）。

        从类属性 id/role/name/desc + cls 本身构建 AgentSpec 并注册。
        """
        from codegenx.ai_service.system_app import get_app
        from codegenx.ai_service.agent.agent_registry import AgentSpec

        registry = get_app().agents
        spec = AgentSpec(
            id=cls.agent_config.id or cls.agent_config.name,
            role=cls.profile.role,
            name=cls.profile.name,
            desc=cls.profile.desc,
            cls=cls,
        )
        registry.register_spec(spec)


    def _resolve_tool_catalog(self) -> list[dict]:
        """按 config.json agents[].tools 解析 function-calling 工具目录。

        - 未配置 tools（None）→ 全量开放：构建全部注册工具（lead agent 统一工具面，
          含 dispatch_agent 编排工具与业务工具，excluded 已在注册时剔除）；
        - 空列表 → 不启用工具（纯文本决策链路）；
        - 非空白名单 → 仅构建白名单内工具（执行型 agent 最小授权）。
        解析失败（如独立调试场景无 SystemApp）时降级为无工具，不阻断 agent 构建。
        """
        allowlist = self.agent_config.tools
        try:
            if allowlist is None:
                return get_tool_registry().build_tool()
            if not allowlist:
                return []
            return get_tool_registry().build_agent_tools(allowlist)
        except Exception as exc:
            log.warning("{} 工具目录解析失败，降级为无工具: {}", self.agent_name, exc)
            return []

    def _reply(self, msg: AgentMessage, target: str, result_text: str) -> AgentMessage:
        """把入参消息改造为回传跳（执行型 agent observation 的统一出口）。

        - task 保留入参原值（追溯），result 承载结果文本；
        - 回传调用方时原样保留 task_id（计划链路看板闭环握手）。
        """
        msg.sender = self.agent_name
        msg.recipient = target
        msg.result = result_text
        msg.success = True
        return msg

    async def _prepare_agent(
        self,
        request: UserAIRequest,
    ) -> None:
        """准备 agent 所需的备料：tools + prompt 参数 + 本轮消息列表。

        system prompt 不在这里构建：每次调 LLM 前由
        CompactionService.token_budget 统一做分区预算后再渲染。
        """
        # 工具目录与执行器：config.json agents[].tools 白名单非空才启用 function calling
        self.tools = self._resolve_tool_catalog()
        # 压缩源保持原始目录：token_budget 的 tools 瘦身仅当次 payload 生效
        # （self.tools 会被写回瘦身结果供本次 LLM 调用），下 step 从原始目录重算，
        # 避免描述截断跨 step 累积、预算恢复后也无法还原
        self._tools_source = self.tools
        self.tool_executor = None
        if self.tools:
            # 配置了工具的 agent 必须拿到执行器，失败直接暴露（fail fast，不静默吞掉）
            self.tool_executor = get_app().tools_executor
        self._prompt_params = await self._prepare_prompt_params(request)
        # 初始化chat_messages
        self._context.init_chat_messages(request.message)

    async def _prepare_prompt_params(self,request:UserAIRequest) -> dict:
        """ 在build prompt之前，子类各自准备 """
        return {}

    def _refresh_step_params(self, request_id: str) -> None:
        """每 step 刷新易变分区（看板随工具调用变化），静态分区不重算。"""
        if self._prompt_params is None:
            return
        if "board_desc" in self._prompt_params:
            self._prompt_params["board_desc"] = self._context.get_board(request_id)

    # ------------------------------------------------------------------ ReAct lifecycle

    async def think(self, messages: list[dict[str, Any]],is_stream:bool,session_state:RuntimeSessionState) -> dict[Any, Any]:
        """思考：调用 LLM 推理，返回 {content, tool_calls}（子类可覆盖定制提示词/工具策略）。"""
        if not is_stream:
            llm_response = await resilient_invoke(
                messages=messages,
                chain = [self.model_name],
                label=self.agent_name,
                tools=self.tools or None,
                temperature=self.agent_config.temperature,
                max_tokens=self.agent_config.max_tokens,
            )

        else:
            llm_response = await invoke_stream_with_revovery(
                self.agent_config, self.tools, messages, session_state,
                agent_context=self._context,
            )
        # log.debug("{},llm_response:{}", self.agent_name, self._trim_response_for_log(llm_response))
        return llm_response

    @staticmethod
    def _trim_response_for_log(llm_response: dict[Any, Any]) -> Any:
        """日志瘦身：reasoning_content 动辄数千 token（曾出现 6k+ token 的审查思考），
        截断展示避免刷屏；content/tool_calls 全量保留。"""
        if not isinstance(llm_response, dict):
            return llm_response
        trimmed = dict(llm_response)
        rc = trimmed.get("reasoning_content")
        if isinstance(rc, str) and len(rc) > 1500:
            trimmed["reasoning_content"] = f"{rc[:1500]}...(truncated, total {len(rc)} chars)"
        return trimmed

    async def act(self, llm_response: dict[str, Any], session_state: RuntimeSessionState,
                  **kwargs) -> ActOutput:
        """行动：遍历 self.actions 链式执行，上一环的 ActOutput 作为下一环的输入。

        - 链路首环输入由 think() 原始返回封装（ActOutput(success=True, output=llm_response)），
          之后各环输入即上一环的返回，类型全程统一为 ActOutput。
        - 任一 Action 返回 success=False 时立即中断，后续 Action 不再执行，
          并整体返回该失败的 ActOutput（由 run 交给调用方判断）。
        """
        if not self.actions:
            log.warning(f"Agent {self.agent_name} 未设置action，返回原始llm response")
            return ActOutput(success=True, output=llm_response,message=f"Agent {self.agent_name} 未设置action，返回原始llm response")

        last_result = ActOutput(success=True, output=llm_response)
        for action in self.actions:
            raise_if_stop_signal(session_state)
            last_result = await action.act(
                ai_message=last_result, session_state=session_state, **kwargs
            )
            log.info("【{} ACT】:   action={} output={}", self.agent_name, type(action).__name__, last_result.model_dump())
            if not last_result.success:
                log.warning("【{} ACT】: action={} 执行失败，中断动作链: {}",
                            self.agent_name, type(action).__name__, last_result.message)

                break
        return last_result

    async def observation(self, msg: AgentMessage, act_output: ActOutput,llm_response:dict[Any, Any]) -> AgentMessage:
        pass


    async def verify(self, observation:AgentMessage) -> tuple[bool, str]:
        """验证：observation 是否产出有效下一跳（子类可覆盖定制校验规则）。返回 (ok, reason)。"""
        if observation.success is not True:
            return False, f"{self.agent_name} 未产出成功的 observation（max_steps 耗尽或持续失败）"
        if not observation.recipient:
            return False, f"{self.agent_name} observation 未指定下一跳 recipient"
        return True, ""


    async def _execute_tool_calls(
        self, tool_calls: list[dict[str, Any]], session_state: RuntimeSessionState,
    ) -> list[dict[str, Any]] | None:
        """执行 tool_calls 并返回 tool 消息列表（_success 记账字段已剔除）。

        返回 None 表示执行器未就绪（run 循环强制结束）。
        所有工具（含 dispatch_agent 派发）统一经 ToolExecutor 执行——
        声明 session_state 参数的工具自动注入会话状态。
        """
        if self.tool_executor is None:
            log.error("【{} THINK】返回了 tool_calls 但工具执行器未就绪", self.agent_name)
            return None
        tool_messages = await self.tool_executor.dispatch_tool_calls(tool_calls, session_state)
        # _success 为执行器内部记账字段，回填对话前剔除，不进 LLM API
        for m in tool_messages:
            m.pop("_success", None)
        return tool_messages

    @staticmethod
    def _normalize_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
        """归一化 tool_call 为扁平结构 {id, name, arguments(dict)}。

        兼容两种来源：非流式 message.to_dict() 的 OpenAI 原生嵌套格式
        （{id, type, function: {name, arguments(字符串)}}）与流式 _safe_build_tool_calls
        的扁平格式（{id, name, arguments(dict)}）。
        """
        fn = tc.get("function") or {}
        name = str(fn.get("name") or tc.get("name") or "")
        raw_args = fn.get("arguments", tc.get("arguments"))
        # arguments 可能是 JSON 字符串（OpenAI 原生）或 dict（流式），统一解析为 dict
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw_arguments": raw_args}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        return {"id": str(tc.get("id") or ""), "name": name, "arguments": args}

    async def run(self, agent_message:AgentMessage,
                  session_state: RuntimeSessionState,
                  is_stream:bool = False,
                  ) -> AgentMessage:
        """ReAct 方式执行：think → act 循环 → verify。

        核心流程仅保留消息收发、LLM 推理与工具执行，全部过程记录到日志；
        agent 边界 span 记录链路树（parent=外层 span 域：turn 或 dispatch_agent）。

        参数:
            session_state: 运行时会话状态，携带 stop_signal/publish_context 等运行时控制
                          （对话上下文已由 __init__ 绑定到 self._context）。
        """
        attributes = {
            "request_id": (agent_message.request_id if agent_message else "") or session_state.request_id,
            "dispatched_by": str(agent_message.sender or "user") if agent_message else "user",
            "task_id": getattr(agent_message, "task_id", None) or None,
            "step_id": getattr(agent_message, "step_id", None) or None,
        }
        task_text = str(agent_message.task or "") if agent_message else ""
        async with start_span(
            "agent", self.agent_name, attributes=attributes, input_data=task_text,
        ) as span:
            observation = await self._run_impl(agent_message, session_state, is_stream, span)
            result_text = str(observation.result or "")
            span.set_output({
                "success": bool(observation.success),
                "result_chars": len(result_text),
            })
            if not observation.success:
                span.set_status(ok=False, error=result_text or "agent 执行失败")
            return observation

    async def _run_impl(self, agent_message:AgentMessage,
                        session_state: RuntimeSessionState,
                        is_stream:bool,
                        span,
                        ) -> AgentMessage:
        """run 主体（span 由 run 包裹后传入，循环步数经 span 上报）。"""
        # 1. 准备 tools / prompt 备料 / init_chat_message（system 由预算器每 step 渲染）
        await self._prepare_agent(session_state.request)
        log.debug("+++++++++++++++++++【{}】 STATR +++++++++++++++++++ ",self.agent_name)
        log.debug("接收到消息:{}", agent_message.show())
        # 2.
        request_id = (agent_message.request_id if agent_message else "") or session_state.request_id
        task_text = agent_message.task
        log.info("agent run 开始: {} | request_id={} | task_len={}", self.agent_name, request_id, len(task_text))

        # 当前任务作为本轮输入追加进对话
        # （leader：init_chat_messages 已剔除快照末条请求原文，此处统一补回，避免重复；
        #   子智能体：SubContext 清空后追加派发任务文本）
        if task_text:
            self._context.chat_messages.append({"role": "user", "content": task_text})

        # 4. ReAct 循环：think →（有 tool_calls 则执行工具回填后继续 think）→
        #    无工具调用且内容非空时走 Action 链（兼容 supervisor 路由解析）+ observation 定跳
        step = 0
        final_text = ""
        act_output: ActOutput | None = None
        # 下一跳消息：observation 成功时为改造后的入参消息；未成功时保持空信封（verify 兜底终答）
        observation: AgentMessage = AgentMessage(
            sender=self.agent_name, recipient="",
            request_id=request_id, session_id=session_state.session_id,
        )
        while step < self.max_steps:
            raise_if_stop_signal(session_state)
            # 每次调 LLM 前统一预算：tools 瘦身 → system 分区 → 消息视图 → 压缩兜底
            # 压缩走 context 多态：leader=SessionContext（Path A 带摘要水位），
            # 子智能体=SubContext（Path B LLM 摘要，只压自己不碰会话真相源）
            self._refresh_step_params(request_id)
            async with start_span("compaction", "token_budget", attributes={"agent": self.agent_name}) as span:
                payload = await get_app().compaction.token_budget(
                    profile=self.profile,
                    prompt_params=self._prompt_params,
                    tools=self._tools_source,
                    context=self._context,
                    context_max_tokens=int(self.agent_config.context_max_tokens or 8000),
                )
                # 仅实际压缩（微压缩/摘要/驱逐/截断/瘦身）保留 span，纯预算分配不落库
                if payload.compacted:
                    span.add_attributes(payload.get_record())
                else:
                    span.discard()
            # 上下文占用每 step 上报（与 span 解耦）：turn 聚合 + 连续超限 streak
            report_context_usage(payload.context_usage, compressed=bool(payload.compact_path))
            self.tools = payload.tools
            self._context.system_prompt = payload.system_prompt
            # 组装职责归 context：system 前置 + 规范化 + tool 配对清理（预算器只管 token）
            messages = await self._context.assemble(payload.payload_messages)
            step += 1
            log.info("【{} Think】:  step {}/{} START", self.agent_name, step, self.max_steps)
            # log.debug("{} 传入llm的chat_message,system_prompt除外：\n{}",self.agent_name,
            #         "\n".join([str(msg) for msg in messages if msg.get("role") != "system"]))
            # LLM 调用前后 hook 分发（纯决策通道：输入输出护轨/限流挂载点；
            # 观测已直埋韧性层——llm span/token/指标不经 hook）
            before_ctx = HookContext(
                event=HookEvent.BEFORE_LLM_INVOKE,
                session=session_state, turn=session_state.activate_turn,
            )
            # before_ctx.data["messages"] = messages
            await hook_manager.dispatch(HookEvent.BEFORE_LLM_INVOKE, before_ctx)
            if before_ctx.action == HookAction.BLOCKED:
                # blocked：以拦截消息作为本轮回复并结束本 agent 循环（限流/预算控制场景）
                blocked_text = before_ctx.message or "LLM 调用被 hook 拦截"
                self._context.chat_messages.append({"role": "assistant", "content": blocked_text})
                observation = self._reply(agent_message, "user", blocked_text)
                break
            response = await self.think(messages,is_stream,session_state)
            after_ctx = HookContext(
                event=HookEvent.AFTER_LLM_INVOKE,
                session=session_state, turn=session_state.activate_turn,
            )
            after_ctx.data["response"] = response
            after_ctx.data["agent_name"] = self.agent_name
            await hook_manager.dispatch(HookEvent.AFTER_LLM_INVOKE, after_ctx)
            if not response:  # TODO reasoning_content 的解析
                self._context.chat_messages.append({"role": "assistant", "content": self.profile.retry_goal})
                continue
            final_text = str(response.get("content") or "")
            # tool_calls 归一化为扁平结构（兼容非流式嵌套格式与流式扁平格式）
            tool_calls = [self._normalize_tool_call(tc) for tc in response.get("tool_calls") or []]
            log.info("【{} Think】:  step {}/{} END | content_len={} tool_calls={}",
                     self.agent_name, step, self.max_steps, len(final_text), len(tool_calls))
            # assistant 消息回写本地对话
            assistant_message: dict[str, Any] = {"role": "assistant", "content": final_text}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
            # leader 的 context 即会话真相源：append 直接落 cm，无需另行回写
            self._context.chat_messages.append(assistant_message)

            if tool_calls:
                # 工具化分支：执行 tool_calls 并回填 tool 消息，继续 think（LLM 自主决定下一步）
                tool_messages = await self._execute_tool_calls(tool_calls, session_state)
                if tool_messages is None:
                    break
                self._context.chat_messages.extend(tool_messages)
                log.info("【{} ACT】: 执行 {} 个工具并回填 tool 消息", self.agent_name, len(tool_calls))
                continue

            if not final_text.strip():
                # 空回复兜底
                self._context.chat_messages.append({
                    "role": "user",
                    "content": "回复为空，请基于任务描述与现有信息给出答复。",
                })
                continue

            raise_if_stop_signal(session_state)
            # 无工具调用 → Action 链兼容层（supervisor 路由解析），随后 observation 定跳
            act_output = ActOutput(success=True, output=response)
            if self.actions:
                act_output = await self.act(response, session_state=session_state)
            observation:AgentMessage = await self.observation(agent_message, act_output,response)

            if observation.success:
                # observation 判定可结束：退出 ReAct 循环
                log.info("【{} OBSERVATION】: 判定完成，退出循环", self.agent_name)
                break
            # observation.success=False 表示需要继续 think
            #（如 supervisor 的 Action 解析失败，或 observation 判定还需进一步处理）
            log.info("【{} OBSERVATION】: 未完成，继续 think", self.agent_name)

        else:
            log.warning("【{} 】: 达到 max_steps={}，强制结束", self.agent_name, self.max_steps)

        # 5. verify：observation 是否产出有效下一跳
        span.add_attributes({"steps": step})
        ok, reason = await self.verify(observation)
        # 6. 返回下一跳消息：observation 已把入参消息改造为下一跳，直接返回；
        #    verify 失败（max_steps 耗尽/无效跳）时按调用方分流兜底：
        #    - 入口 agent（sender=user，如 supervisor）→ 终答跳兜底；
        #    - 执行 agent → 失败回传调用方（supervisor 审查后决定重试/换路/终答），
        #      保留 task_id 闭环握手，避免看板任务永久 in_progress。
        if not ok:
            caller = str(agent_message.sender or "user")
            observation.sender = self.agent_name
            observation.recipient = caller
            if caller == "user":
                observation.result = "执行失败，请稍后重试"
                observation.task_id = None
                observation.step_id = None
            else:
                observation.task = agent_message.task  # task 保留原值（回传协议追溯）
                observation.result = f"执行失败：{reason}" if reason else "执行失败，请稍后重试"
                observation.task_id = agent_message.task_id  # 看板闭环握手
                observation.step_id = agent_message.step_id
                observation.success = False  # 消息级失败标记（入史/派发结果回填消费）
        log.info("【{} 】结束: {} | steps={} | ok={} | reason={}", self.agent_name, request_id, step, ok, reason)
        log.debug("+++++++++++++++++++【{}】 END +++++++++++++++++++ ", self.agent_name)

        return observation

    # ------------------------------------------------------------------ message I/O
    
    async def receive(self, msg: AgentMessage, session_state: RuntimeSessionState) -> AgentMessage | None:
        """接收来自其他 agent 或外部的消息，执行 run() 并转发下一跳。

        注意：全局消息路由已收归 AutoPlanManager，此处仅为独立测试或手动调用保留。
        """
        log.info(
            "agent 消息接收: {} <- {} | request_id={}",
            self.agent_name, msg.sender, msg.request_id,
        )
        reply = await self.run(msg, session_state)
        if reply:
            if reply.recipient == "user":
                get_app().message_bus.publish_runtime_event(
                    session_state.publish_context,
                    event_type=AgentEventType.REQUEST_COMPLETED,
                    data={"content": reply.result},
                    state=AgentState.COMPLETED,
                )
            else:
                await self.send(reply, session_state)
        return msg

    async def send(
            self,
            msg: AgentMessage,
            session_state: RuntimeSessionState,
            **kwargs,
    ) -> bool:
        """向指定 name 的 agent 发送消息并触发其 receive → run 链路。

        从 AgentRegistry 取 AgentSpec.cls() 实例化目标 agent，再调 receive。
        """
        receiver = msg.recipient
        if not receiver:
            raise ValueError(f"未设置接收方：{msg}")

        spec = get_app().agents.get(receiver)
        if spec is None or spec.cls is None:
            raise ValueError(f"目标 agent 未注册或无 cls: {receiver}")

        target_agent = spec.cls()
        await target_agent.receive(msg, session_state, **kwargs)
        log.info(
            "agent 消息发送: {} -> {} | request_id={}",
            msg.sender, msg.recipient, msg.request_id,
        )
        return True
