"""SessionContext —— 纯会话状态（P2 瘦身，docs/SystemApp架构设计.md §4.2）。

只留真会话状态：ids、system_prompt、chat_messages、
摘要阈值状态（SummaryState）。原成员对象（MemoryManager/TaskManager/
SessionSummaryService/CompactionEngine/ContextAssembler/SkillLoader）全部服务化，
经 get_app() 的全局服务委托完成；压缩熔断器由 CompactionService 服务级持有，
会话不绑定。
"""
from dataclasses import field, dataclass
from typing import Any, Dict, AsyncGenerator
import asyncio
from codegenx.ai_service.agent.agent_schema import AgentEvent, AgentState, AgentEventType
from codegenx.ai_service.context.common_context import CommonContext
from codegenx.ai_service.compact import microcompact_messages, estimate_tokens
from codegenx.ai_service.compact.large_output import persist_large_output
from codegenx.ai_service.tools.base import ToolResult
from codegenx.ai_service.trace import start_span
from codegenx.ai_service.utils.context_utils import rough_tokens, ensure_app_workdir
from shared import log

# ── 会话摘要阈值（原 SessionSummaryService 模块常量，属会话级判定，随谓词下沉）────
MIN_TOKENS_TO_INIT = 6000           # 上下文达到该规模后开始跟踪
MIN_TOKENS_BETWEEN_UPDATES = 150    # 新增 token 超过该值才允许再次提取
MIN_MESSAGES_BETWEEN_UPDATES = 4    # 或新增内存消息（index 增量）超过该值


class SummaryState:
    """会话摘要阈值状态（原 SessionSummaryService 实例字段，迁入 SessionContext 持有）。

    这是真·会话状态（非伪会话对象）：服务全局一份，此状态随会话生灭。
    判定谓词落在本类：阈值状态与判断它的逻辑同处，服务只保留「执行」。

    水位语义：extract_msg_idx 是内存 chat_messages 的 index 水位，表示
    「前 extract_msg_idx 条已摘要过」。compact_after_turn 后台按内存快照
    做增量摘要并推进水位；_session_memory_compact 压缩重建列表后水位失效，
    由 _extract 检测（extract_msg_idx >= len(messages)）回退 0 全量重摘。
    """

    def __init__(self) -> None:
        # 阈值状态
        self.initialized = False
        self.tokens_at_last_extract = 0
        # 上次摘要消费到的内存 chat_messages 列表 index 水位（随会话生灭，不持久化）
        self.extract_msg_idx = 0
        # 并发保护：同一会话的提取任务不重叠
        self.extracting = False
        self.extract_task: asyncio.Task | None = None

    def should_extract(self, messages: list[dict]) -> bool:
        """是否应触发一次后台摘要提取（纯会话状态判定，无外部依赖）。"""
        if self.extracting:
            return False  # 永不重叠

        current = rough_tokens(messages)

        if not self.initialized:
            if current < MIN_TOKENS_TO_INIT:
                return False
            self.initialized = True

        growth = current - self.tokens_at_last_extract
        if growth < MIN_TOKENS_BETWEEN_UPDATES:
            return False

        return (
            len(messages) - self.extract_msg_idx >= MIN_MESSAGES_BETWEEN_UPDATES
            or growth >= MIN_TOKENS_BETWEEN_UPDATES * 3
        )

    def note_trigger(self, tokens: int) -> None:
        """记录本次触发的 token 记账基线。extract_msg_idx 由 _extract 完成后推进。"""
        self.tokens_at_last_extract = int(tokens)


class SessionMetadata:
    safety_path:list[str]
    metadata: dict[str, Any]

    def set_safety_path(self, user_id:str,app_id:str) -> None:
        self.safety_path = [str(ensure_app_workdir(user_id, app_id))]

@dataclass
class SessionContext(CommonContext):
    """
    负责一个session的上下文管理
    提示词，组装
    历史对话
    token管控
    压缩
    """

    # ── 身份（定位数据与目录）────────────────────────────────
    session_id: str = ""
    app_id: str = ""
    # 会话归属用户，决定 .data/{userId}/{appId} 两级路径
    user_id: str = ""
    db_name: str | None = None

    # 系统提示词
    system_prompt: str = ""
    # 整个会话的聊天历史（唯一大对象）
    chat_messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    # 会话摘要阈值状态
    summary_state: SummaryState = field(default_factory=SummaryState)

    def __post_init__(self) -> None:
        # 元信息载入
        self.metadata.set_safety_path(self.user_id, self.app_id)
        log.info("{} SessionContext 启动完毕", self.session_id)

# ----------------------------------------------------------------------
# chat_message 初始化
# ----------------------------------------------------------------------
    def init_chat_messages(self,user_msg:str) -> None:
        """turn 初始组装聊天上下文。

        chat_messages 来自 turn 结束后的快照（或上轮内存列表），request 一进入即装载。

        顺序约束（水位语义要求）：
        1. 去末条：本轮用户请求已作为当前输入，run() 会以 task_text 补回；
        2. 先消费水位再过滤 tool——水位是全量空间（含 tool）的 index：
           有 SUMMARY.md → 按水位截掉已覆盖前缀、prepend 合成对（与 compact Path A
           同语义：摘要替代原文）；无 SUMMARY.md → 不截、水位归 0（宁全量重摘不丢内容）；
        3. 最后过滤 tool：只保留 user/assistant 文本消息（role/content），
           跨轮蒸馏；tool 原文可经工具重调回捞，结论已在 assistant 文本与摘要中。
        """
        # 1. 去末条：本轮用户请求已作为当前输入
        self.chat_messages = self.chat_messages[:-1]

        # 2. 恢复水位 + 摘要替代已覆盖前缀（全量空间操作，必须在过滤前）
        self._restore_summary_state()
        self._inject_summary_from_file()

        # 3. 过滤 tool：水位已消费完毕，进入过滤空间
        self.chat_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in self.chat_messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        log.debug(
            "{} init_chat_messages: {} 条，水位 {}",
            self.session_id, len(self.chat_messages), self.summary_state.extract_msg_idx,
        )

    def _restore_summary_state(self) -> None:
        """从 summary_state.json 恢复 SummaryState 水位（compact 后会重置落盘）。

        水位是快照全量空间（含 tool）的 index，仅应在过滤 tool 前消费（见
        init_chat_messages 顺序约束）；水位超列表由截断处 min() 兜底。
        """
        from codegenx.ai_service.system_app import get_app
        state_data = get_app().summary.load_state(
            user_id=self.user_id, app_id=self.app_id, session_id=self.session_id,
        )
        if state_data is None:
            return
        self.summary_state.extract_msg_idx = int(state_data.get("extract_msg_idx", 0))
        self.summary_state.initialized = bool(state_data.get("initialized", False))
        self.summary_state.tokens_at_last_extract = int(state_data.get("tokens_at_last_extract", 0))

    def _inject_summary_from_file(self) -> None:
        """SUMMARY.md 覆盖已摘要前缀：按水位截掉原文 → prepend 合成对 → 重置落盘。

        与 compact Path A 同语义（摘要替代原文），只是发生在 turn 开始的恢复路径。
        防御：无摘要时不截——水位>0 而摘要缺失属不一致状态（提取失败/被清理），
        保留原文全量重摘，宁可冗余不可丢内容；水位归 0 后全量空间继续有效。
        """
        from codegenx.ai_service.system_app import get_app
        app = get_app()
        summary = app.summary.load(
            user_id=self.user_id, app_id=self.app_id, session_id=self.session_id,
        )
        if not summary:
            self.summary_state.extract_msg_idx = 0
            self.summary_state.tokens_at_last_extract = estimate_tokens(self.chat_messages)
            log.debug("{} 无 SUMMARY.md，不截前缀，水位归 0", self.session_id)
            return

        # 关键：chat_messages 前 extract_msg_idx 条的内容已被 SUMMARY.md 覆盖 → 截掉；
        # 水位覆盖至末条（wm >= len）时截空，仅留合成对
        watermark = self.summary_state.extract_msg_idx
        if watermark > 0:
            self.chat_messages = self.chat_messages[min(watermark, len(self.chat_messages)):]
            log.debug(
                "{} 摘要注入：按水位 {} 截掉已覆盖前缀，剩余 {} 条",
                self.session_id, watermark, len(self.chat_messages),
            )

        user_msg, assistant_msg = app.summary.build_summary_injection_pair(summary)
        self.chat_messages = [user_msg, assistant_msg] + self.chat_messages

        # 合成对自身算"已摘要的 2 条" → 水位重置为 2；列表已大幅缩短，
        # 提取基线按新列表重估（否则 growth 长时间为负，提取被延迟）
        self.summary_state.extract_msg_idx = 2
        self.summary_state.tokens_at_last_extract = estimate_tokens(self.chat_messages)
        app.summary.save_state(
            self.summary_state,
            user_id=self.user_id, app_id=self.app_id, session_id=self.session_id,
        )
        app.summary.clear(user_id=self.user_id, app_id=self.app_id, session_id=self.session_id)

        log.info(
            "{} 注入 SUMMARY.md 合成对（截 {} 条已覆盖前缀 → {} 条）",
            self.session_id, watermark, len(self.chat_messages),
        )

    def get_task_bord(self, request_id: str = "") -> str:
        """从 TaskBoardService 拉取当前请求看板并重渲染进

        看板按 request 单文件隔离，仅渲染当前请求的任务（历史请求任务不进 prompt）。
        渲染由无状态服务完成。
        """
        from codegenx.ai_service.system_app import get_app
        tasks = getattr(get_app(), "tasks", None)
        if tasks is None:
            return ""
        return tasks.get_board(
            user_id=self.user_id, app_id=self.app_id, session_id=self.session_id, request_id=request_id
        )

    def get_board(self, request_id: str = "") -> str:
        """预算层统一入口：会话看板渲染（与 get_task_bord 同源）。"""
        return self.get_task_bord(request_id)

    def add_user_message(self,message):
        if isinstance(message,dict):
            self.chat_messages.append(message)
        if isinstance(message,str):
            self.chat_messages.append({"role":"user","content":message})


    def add_assistant_message(self, message):
        if isinstance(message,dict):
            self.chat_messages.append(message)
        if isinstance(message,str):
            self.chat_messages.append({"role": "assistant", "content": message})

    def add_tool_message(self, message):
        if isinstance(message,dict):
            self.chat_messages.append(message)
        if isinstance(message,str):
            self.chat_messages.append({"role": "tool", "content": message})

    async def persist_large_output(self,tool_call:Dict[str, Any], output:ToolResult) -> str:

        data =  output.data or ""
        log.debug("大的输出持久化：{}",len(data))
        return persist_large_output(tool_call=tool_call, output=data,user_id=self.user_id,app_id=self.app_id,session_id=self.session_id)

# ------------------------------------------------------------------
# 压缩
# ------------------------------------------------------------------

    async def compact(self, token_budget: int | None = None) -> AsyncGenerator[dict[str, Any], None]:
        """每个 step 后 / llm 运行中，供 recovery 策略调用：
        仅做 token 检查 + full compaction，不触发 session summary extraction。

        compact 重建 chat_messages 后：
          - Path A（session_memory）：SUMMARY.md 内容已以合成对形式融进 chat_messages，
            旧摘要文件需清理（否则下次 compact 又会套娃合成）；SummaryState 重置落盘。
          - Path B / 兜底截断：chat_messages 被重建但不走 SUMMARY.md，
            SummaryState 水位失效 → 回退 0 重置，落盘。
        """
        from codegenx.ai_service.system_app import get_app

        app = get_app()
        self.chat_messages, result = await app.compaction.compact_if_needed(
            self.chat_messages,
            summary_loader=lambda: app.summary.load(
                user_id=self.user_id, app_id=self.app_id, session_id=self.session_id
            ),
            summary_watermark=self.summary_state.extract_msg_idx,
            threshold=token_budget,
        )

        if result is not None:
            log.debug("进行step的压缩了 path={}", result.path_used)
            # 压缩后 chat_messages 已重建 → SummaryState 水位必失效，统一重置
            if result.path_used == "session_memory":
                # Path A：合成对 = [user, assistant] 2 条，水位跳过它们
                self.summary_state.extract_msg_idx = 2
                self.summary_state.initialized = True
                log.debug("Path A：水位重置为 2")
            else:
                # Path B / 兜底截断：列表被重建但不走 SUMMARY.md，水位回退 0
                self.summary_state.extract_msg_idx = 0
            # PathA 已经被使用了，SUMMARY.md 内容已融进合成对 → 清理旧文件，避免下次 compact 套娃
            # PathB 已经是新的chat_message了，不管有没有摘要已经对不上了，清理旧文件 
            app.summary.clear(
                user_id=self.user_id, app_id=self.app_id, session_id=self.session_id,
            )
            # 重置基线 token 计数
            self.summary_state.tokens_at_last_extract = estimate_tokens(self.chat_messages)
            # 重置后立即落盘，保证会话恢复时状态一致
            app.summary.save_state(
                self.summary_state,
                user_id=self.user_id, app_id=self.app_id, session_id=self.session_id,
            )

            yield {
                    "path_used": result.path_used,
                    "tokens_before": result.tokens_before,
                    "tokens_after":  result.tokens_after,
                    "messages_removed": result.messages_removed,
                }
        else:
            log.info("compact: compaction skipped (not needed)")

    async def session_summary_extract(self):
        """整个 turn 结束后：阈值门控下触发会话摘要后台提取（非阻塞，属压缩边界）。

        判定谓词在 SummaryState（会话级状态自查）；摘要输入按内存 chat_messages
        快照切片（extract_msg_idx 为内存 index 水位），不依赖 DB。
        compaction span 在此记录（触发即记；提取本身在后台异步完成，
        LLM 调用指标由 llm 客户端层喂入，不在此追踪）。
        """
        from codegenx.ai_service.system_app import get_app

        app = get_app()
        if self.summary_state.should_extract(self.chat_messages):
            # 摘要触发 compaction span（root=True 挂 turn；未触发阈值不落 span）
            async with start_span(
                "compaction", "session_summary", root=True,
                attributes={"session_id": self.session_id, "messages": len(self.chat_messages)},
            ):
                app.summary.fire_extract(
                    self.summary_state,
                    self.chat_messages,
                    user_id=self.user_id,
                    app_id=self.app_id,
                    session_id=self.session_id,
                    trigger="turn_end",
                )
            log.debug("session summary 后台提取已触发")
        else:
            log.debug("session summary 未触发提取阈值")

    async def ensure_summary_before_unload(self) -> None:
        """swap-out/空闲回收前保证会话摘要跟上内存消息（上下文工程排期②）。

        强制触发（绕过增长阈值）：回收/卸载后内存列表消失，未摘要内容只能
        等下次会话重摘；此处先补一轮保证摘要连续。

        fire-and-forget：后台任务按内存快照切片独立运行；清理锁内不做 IO
        与 LLM 调用（避免阻塞全池）；无新消息时后台任务自然空转退出。
        """
        from codegenx.ai_service.system_app import get_app

        if not self.chat_messages:
            return
        app = get_app()
        summary = getattr(app, "summary", None)
        if summary is None:
            return
        summary.fire_extract(
            self.summary_state,
            self.chat_messages,
            user_id=self.user_id,
            app_id=self.app_id,
            session_id=self.session_id,
            trigger="session_end",  # 卸载/回收边界触发，trace 根 span 记 session_end
        )
        log.info(
            "会话 {} 卸载前强制摘要已触发（列表 {} 条）",
            self.session_id, len(self.chat_messages),
        )

# ----------------------------------------------------------------
# 前端接口
# ----------------------------------------------------------------
    # TODO
    def token_status(self) -> dict:
        """
        返回上下文窗口token使用情况字典，用于前端展示
        """
        from codegenx.ai_service.compact import calculate_warning_state
        return calculate_warning_state(self.chat_messages)
