import time
import asyncio
from codegenx.ai_service.agent.agent_schema import AgentState, AgentEventType
from codegenx.ai_service.agent.agents.agent import AgentMessage
import traceback
from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState, TurnStoppedError, raise_if_stop_signal
from codegenx.ai_service.hook import HookEvent, hook_manager, HookContext
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.trace import session_end as trace_session_end
from codegenx.ai_service.trace import turn_end as trace_turn_end
from codegenx.ai_service.trace import turn_error as trace_turn_error
from codegenx.ai_service.trace import turn_start as trace_turn_start
from shared import log


def _message_bus():
    """运行时 lazy 获取 MessageBus（避免模块 import 时 app 尚未初始化）。"""
    return get_app().message_bus



class AutoPlanManager:
    """
    请求执行入口 —— 单跳直通（多跳路由已随 agent-as-tool 退役）。

    核心职责：
    1. 把用户消息包装成 AgentMessage(sender="user") 投给 lead agent（leader）
    2. lead 内部 ReAct 工具循环完成全部编排：子智能体经 dispatch_agent 工具
       内联执行，结果以 tool result 回填，不产生跨 agent 消息跳
    3. lead 返回终答跳（recipient=="user"），抽取文本下发收尾

    调度链路只剩 user → lead → user；单次 run 步数由 lead 自身 max_steps
    约束（原全局 turn_max 轮次上限已随多跳路由一并移除）。
    """

    async def execute_request(self, session_state: RuntimeSessionState):
        request_id = session_state.request_id
        activate_turn = session_state.activate_turn
        activate_turn.state = AgentState.RUNNING
        activate_turn.started_at = time.time()

        # trace turn 生命周期直调（观测直埋，不寄生 hook 通道）
        trace_turn_start(session_state, activate_turn)

        try:
            raise_if_stop_signal(session_state)

            # ── 用户消息包装 + 写入会话历史真相源 ──
            user_text = str(getattr(session_state.request, "message", "") or "")
            if session_state.context is not None:
                session_state.context.add_user_message(user_text)
            msg = AgentMessage(
                sender="user",
                recipient="leader",
                task=user_text,
                request_id=request_id,
                session_id=session_state.session_id,
            )

            # ── 单跳直通：lead 内部 ReAct 工具循环完成编排（子智能体经
            #    dispatch_agent 内联执行），返回终答跳 recipient=="user"。
            #    round_counter 保留单次递增（下游 persist/monitor 以其作
            #    turn_id 口径），恒为 1。
            activate_turn.round_counter += 1
            log.info("─────────────── [TURN] user ──► leader ─────────────── | request={} | task: {}",
                     request_id, user_text.replace("\n", " ")[:80])

            spec = get_app().agents.get("leader")
            if spec is None or spec.cls is None:
                log.error("lead agent 未注册或无 cls: name=leader")
                await self._reply_and_finish(session_state, "执行失败：找不到智能体 leader")
                return
            # 实例必须每请求新建、禁止缓存复用；context=会话真相源（SessionContext）
            cm = session_state.context
            if cm is not None:
                cm.db_name = session_state.db_name  # 同步数据库名到 context
            agent = spec.cls(context=cm)
            next_msg = await agent.run(msg, session_state, is_stream=True)

            # 防御性兜底：正常路径 lead 必返终答跳；空跳/非 user 跳按终答收尾，避免静默丢答复
            if next_msg is None or not getattr(next_msg, "recipient", None):
                log.warning("lead.run 返回空下一跳，兜底收尾 | request={}", request_id)
                next_msg = msg  # 以任务文本兜底
            elif next_msg.recipient != "user":
                log.warning("lead.run 返回非 user 跳 recipient={}，按终答兜底收尾 | request={}",
                            next_msg.recipient, request_id)

            final_text = self._extract_final_text(next_msg)
            log.info("═══════════════ TURN COMPLETE ═══════════════ | request={} | final_len={}",
                     request_id, len(final_text))
            # 终答已由流式 chunk 下发、且已由 leader 回写会话历史，此处只落库 +
            # 发布完成事件：SSE 消费端以 REQUEST_COMPLETED 为唯一终止条件，
            # 缺失则连接悬挂到客户端读超时（不重复 add_assistant_message 避免入史两份）
            await AutoPlanManager._persist_assistant_message(session_state, final_text)
            activate_turn.state = AgentState.COMPLETED
            await _message_bus().publish_runtime_event(
                session_state.publish_context,
                event_type=AgentEventType.REQUEST_COMPLETED,
                state=AgentState.COMPLETED,
            )

        except TurnStoppedError as exc:
            traceback.print_exc()
            activate_turn.error_text = str(exc)
            activate_turn.state = AgentState.STOPPED
            await _message_bus().publish_runtime_event(
                session_state.publish_context, event_type=AgentEventType.REQUEST_STOPPED,
                data={"reason": str(exc)}, state=AgentState.STOPPED,
            )
            log.info("┌─ TurnStoppedError ─┐ request={} round={} reason={}", request_id, activate_turn.round_counter, exc)
            log.debug("{},{},{},{}", request_id, activate_turn.round_counter, " TurnStoppedError：", exc)
        except asyncio.CancelledError:
            traceback.print_exc()
            reason = str(session_state.stop_reason or "user-stop")
            activate_turn.error_text = reason
            activate_turn.state = AgentState.STOPPED
            await _message_bus().publish_runtime_event(
                session_state.publish_context,
                event_type=AgentEventType.REQUEST_STOPPED,
                data={"reason": reason},
                state=AgentState.STOPPED,
            )
            log.info("┌─ CancelledError ─┐ request={} reason={}", request_id, reason)
            log.debug("{},{},{}", request_id, activate_turn.round_counter, " CancelledError")
            raise
        except Exception as exc:
            traceback.print_exc()
            log.opt(exception=True).error("_execute_request failed: {}", exc)
            activate_turn.error_text = str(exc)
            activate_turn.state = AgentState.FAILED
            trace_turn_error(session_state, str(exc))
            await _message_bus().publish_runtime_event(
                session_state.publish_context, event_type="Error", data=str(exc), state=AgentState.FAILED,
            )
            log.info("┌─ Exception ─┐ request={} round={} exc={}", request_id, activate_turn.round_counter, exc)
            log.debug("{},{}, Exception:{}", request_id, activate_turn.round_counter, exc)
        finally:
            activate_turn.finished_at = time.time()
            # turn 边界：触发后台会话摘要提取（非阻塞；失败不影响本轮收尾）
            try:
                await session_state.context.session_summary_extract()
            except Exception:
                log.exception("compact_after_turn 执行异常（非致命）")
            # 注：子智能体消息全部内联为 lead 的 tool result，本层无跨 agent
            # 消息需清理；SSE 订阅队列由消费端 unsubscribe 管理。
            await get_app().session_io.save_turn_chat_message_snapshot(
                session_state.context.chat_messages,
                user_id=session_state.user_id,
                app_id=session_state.app_id,
                session_id=session_state.session_id,
            )
            await hook_manager.emit(
                HookEvent.TURN_END,
                HookContext(event=HookEvent.TURN_END, session=session_state, turn=activate_turn),
            )
            # trace turn 收口直调（emit 后：等记忆信号等监听器完成再 flush，等价原 priority=90）
            trace_turn_end(session_state, activate_turn)
            log.info("═══════════════ TURN END ═══════════════ | request={} final_state={} total_rounds={}",
                     request_id, activate_turn.state.value, activate_turn.round_counter)
            log.debug("OnTurnEnd {},{}", request_id, activate_turn.round_counter)

    @staticmethod
    def _extract_final_text(msg: AgentMessage) -> str:
        """从最终面向用户的 AgentMessage 里抽取纯文本。

        终答跳 result 承载答复文本；兜底降级路径（空跳等）可能只有 task，
        用 task 兜底返回已派发的任务文本。
        """
        if msg is None:
            return ""
        return str(msg.result or msg.task or "")

    @staticmethod
    async def _reply_and_finish(session_state: RuntimeSessionState, content: str) -> None:
        """短路分支收尾：把面向用户的回复经 REQUEST_COMPLETED 事件下发。"""
        session_state.activate_turn.state = AgentState.COMPLETED
        # 最终答复写入会话历史真相源（用户可见的最终轮）
        if session_state.context is not None:
            session_state.context.add_assistant_message(content)
        # 终答入库须早于 TURN_END（记忆漏斗/warm_extract 按表中完整轮次消费）
        await AutoPlanManager._persist_assistant_message(session_state, content)
        await _message_bus().publish_runtime_event(
            session_state.publish_context,
            event_type=AgentEventType.REQUEST_COMPLETED,
            data={"content": content},
            state=AgentState.COMPLETED,
        )

    @staticmethod
    async def _persist_assistant_message(
        session_state: RuntimeSessionState, content: str
    ) -> None:
        """把面向用户的终答落 chat_message（失败非致命，不影响本轮下发）。

        表内完整轮次 = user 行 + assistant 行：记忆 warm_extract 以
        「读到的增量能被 assistant 收尾」判定完整轮次，缺 assistant 行会
        永远凑不出轮次、提取空转；前端会话历史也只展示表内消息。
        """
        text = str(content or "").strip()
        if not text:
            return
        try:
            from codegenx.ai_service.chat_message import get_chat_message_store

            await get_chat_message_store().append_message(
                session_state.user_id,
                str(session_state.app_id),
                session_state.session_id,
                {"role": "assistant", "content": text},
            )
        except Exception as exc:  # noqa: BLE001 —— 入库失败不阻断对话
            log.warning("assistant 消息入库失败（不影响对话）: {}", exc)

