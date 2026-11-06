"""
会话摘要服务（上下文工程）—— 从记忆系统整体移交而来。

职责边界：会话内摘要服务于「压缩边界」，属于上下文工程；
跨会话的持久记忆（hot/warm）属于记忆系统（memory/ 包），二者不再耦合。

机制（原 memory/session.py，行为保持不变）：
  SummaryState.should_extract() → token 增长 + 内存 index 增量阈值判断（会话级谓词）
  fire_extract()     → 非阻塞后台 Task 做摘要，写 session 目录下的摘要文件
  load()             → 同步读取当前摘要（CompactionEngine 压缩前调用，
                        作为 Path A 快速通道的上下文恢复来源）

水位线：SummaryState.extract_msg_idx 记录内存 chat_messages 的 index 水位，
摘要输入按该水位从内存快照切片读取。先写 SUMMARY.md 后推进水位（安全序，
失败只会重复摘要、不会漏摘要）；单写者 = 摘要后台任务。Path A 压缩重建
列表后水位失效，由 _extract 检测回退 0 全量重摘。

阈值判定谓词（should_extract）与阈值状态 SummaryState 同处会话层，本服务
只保留「执行」：LLM 提取、读内存快照、写盘。
"""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from contextlib import suppress
from pathlib import Path
from codegenx.ai_service.context.session_context import SummaryState
from shared import log
from shared.constants import get_session_dir
from codegenx.ai_service.component import BaseComponent, ComponentType
from codegenx.ai_service.compact.prompt import get_compact_user_summary_message
from codegenx.ai_service.llm.async_client import get_llm
from codegenx.ai_service.llm.errors import classify_llm_error
from codegenx.ai_service.llm.resilience import get_breaker, resilient_invoke
from codegenx.ai_service.utils.context_utils import rough_tokens
from codegenx.ai_service.utils.config import config, split_model_chain

# 单次摘要提取最多喂入的消息条数（水位增量超限时从最老开始截断，剩余下轮补摘）
MAX_EXTRACT_MESSAGES = config.compact.summary_extract_max_messages or 200


def get_session_summary_path(user_id: str, app_id: str, session_id: str) -> Path:
    """会话摘要文件：.data/{userId}/{appId}/session/{session_id}/SUMMARY.md"""
    return get_session_dir(user_id, app_id) / session_id / "SUMMARY.md"


def get_session_summary_state_path(user_id: str, app_id: str, session_id: str) -> Path:
    """会话摘要水位状态文件：.data/{userId}/{appId}/session/{session_id}/summary_state.json"""
    return get_session_dir(user_id, app_id) / session_id / "summary_state.json"


# ── 会话摘要模板与提取提示词（原 memory/prompts.py 的 session 部分） ─────────

SESSION_SUMMARY_TEMPLATE = """\
# Session Title
_A short and distinctive 5-10 word descriptive title for the session._

# Current State
_What is actively being worked on right now? Pending tasks not yet completed. Immediate next steps._

# Task Specification
_What did the user ask to build or find? Key design decisions or explanatory context._

# Key Files and Topics
_What are the important files, functions, or topics discussed? Brief notes on each._

# Errors and Corrections
_Errors encountered and how they were fixed. What approaches failed and should not be tried again._

# Learnings
_What has worked well? What should be avoided? Do not duplicate items from other sections._

# Key Results
_If the user asked for a specific output (answer, table, document), include the exact result here._

# Worklog
_Step by step, what was attempted and done? Very terse summary for each step._
"""


def build_summary_extraction_prompt(
    messages: list[dict],
    current_notes: str,
    notes_path: str,
) -> str:
    """构建会话摘要更新的系统提示词。"""
    conversation = _format_messages_for_extraction(messages)
    return (
        f"IMPORTANT: These instructions are NOT part of the actual conversation. "
        f"Do NOT reference note-taking in the notes content.\n\n"
        f"Based on the conversation below, update the session notes at {notes_path}.\n\n"
        f"Current notes:\n<current_notes>\n{current_notes or '(empty)'}\n</current_notes>\n\n"
        f"Conversation:\n<conversation>\n{conversation}\n</conversation>\n\n"
        f"Update every section that has new information. Be terse but info-dense. "
        f"Preserve all section headers and italic description lines exactly."
    )


def _format_messages_for_extraction(messages: list[dict]) -> str:
    """格式化为提取 prompt 输入（调用方已传水位后的精确增量切片，此处不再截取）。"""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "; ".join(str(item.get("content", "")) for item in content)
        if isinstance(content, str) and content.strip():
            parts.append(f"[{role}]: {content[:400]}")
        for tc in msg.get("tool_calls", []):
            parts.append(f"[tool_call]: {tc.get('name', '?')}({tc.get('input', {})})")
    return "\n".join(parts)


class SessionSummaryService(BaseComponent):
    """
    全局无状态会话摘要服务（P2 服务化，docs/SystemApp架构设计.md §4.3）。

    生命周期：
      1. 会话层 SummaryState.should_extract() 判定通过后，compact_after_turn()
         /ensure_summary_before_unload() 调 fire_extract()。
      2. CompactionService 压缩前调用 load(ids) 取最新摘要（Path A 快速通道）。
    阈值状态与判定谓词均由会话层（SessionContext.summary_state: SummaryState）持有。

    模型：初始化时从 compact.model_name 解析（与压缩同族共用小模型配置，
    缺省回落默认模型），启动钩子中预热客户端与熔断器。
    """

    name = ComponentType.SESSION_SUMMARY

    def __init__(self, system_app=None) -> None:
        BaseComponent.__init__(self, system_app)
        # 初始化即解析摘要模型链（压缩族共用 compact.model_name，逗号分隔，空则默认模型）
        self._model_chain: list[str] = split_model_chain(
            config.compact.model_name, config.get_default_model(),
        )

    def init_app(self, system_app) -> None:
        self.system_app = system_app

    async def async_before_start(self) -> None:
        """预热摘要模型的客户端与熔断器（构造即注册，不发起网络请求；失败不阻断启动）。"""
        for model in self._model_chain:
            with suppress(Exception):
                get_llm(model)
                get_breaker(model)
                log.info("[session_summary] 摘要模型客户端已预热: {}", model)

    @property
    def model_chain(self) -> list[str]:
        return list(self._model_chain)


    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def load(self, *, user_id: str, app_id: str, session_id: str) -> str:
        """同步读取当前会话摘要文件。"""
        path = get_session_summary_path(user_id, app_id, session_id)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def save_state(
        self, state: SummaryState, *,
        user_id: str, app_id: str, session_id: str,
    ) -> None:
        """SummaryState 水位落盘（摘要推进或 compact 重置后调用，会话恢复时可还原）。"""
        path = get_session_summary_state_path(user_id, app_id, session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "extract_msg_idx": state.extract_msg_idx,
                "initialized": state.initialized,
                "tokens_at_last_extract": state.tokens_at_last_extract,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("[summary] 水位落盘失败: {}", exc)

    def load_state(
        self, *, user_id: str, app_id: str, session_id: str,
    ) -> dict | None:
        """读取 SummaryState 落盘文件；不存在或损坏返回 None。"""
        path = get_session_summary_state_path(user_id, app_id, session_id)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return json.loads(raw) if raw else None
        except (OSError, json.JSONDecodeError):
            return None

    def clear(self, *, user_id: str, app_id: str, session_id: str) -> None:
        """compact Path A 重建列表后清理 SUMMARY.md 与 summary_state.json（内容已融进合成对）。"""
        for p in (
            get_session_summary_path(user_id, app_id, session_id),
            get_session_summary_state_path(user_id, app_id, session_id),
        ):
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("[summary] 清理摘要文件失败 {}: {}", p.name, exc)

    @staticmethod
    def build_summary_injection_pair(summary: str) -> tuple[dict, dict]:
        """构造合成 user/assistant 对（注入 chat_messages 首部，格式与 compact Path A 一致）。"""
        return (
            {"role": "user", "content": get_compact_user_summary_message(summary)},
            {"role": "assistant",
             "content": "I'll continue from where we left off. I have the context from the previous session."},
        )

    def fire_extract(
        self,
        state: SummaryState,
        messages: list[dict],
        *,
        user_id: str,
        app_id: str,
        session_id: str,
        trigger: str = "turn_end",
    ) -> None:
        """调度非阻塞后台摘要任务。

        仅做记账（更新阈值基线）+ 起后台任务；should_extract 判定在会话层
        （SummaryState）完成。messages 浅拷贝后传入后台任务，主对话可继续变更；
        trigger 传触发事件（turn_end/session_end），透传到 trace 根 span 的 op_type。
        """
        if state.extracting:
            return
        state.extracting = True
        state.note_trigger(rough_tokens(messages))
        messages_snapshot = list(messages)
        state.extract_task = asyncio.create_task(
            self._extract(
                state, messages_snapshot,
                user_id=user_id, app_id=app_id, session_id=session_id,
                trigger=trigger,
            )
        )

    # ── 内部提取 ──────────────────────────────────────────────────────────────

    async def _extract(
        self,
        state: SummaryState,
        messages_snapshot: list[dict],
        *,
        user_id: str,
        app_id: str,
        session_id: str,
        trigger: str = "turn_end",
    ) -> None:
        """后台任务：按内存 index 水位切快照增量 → 覆盖写 SUMMARY.md → 推进水位；失败非致命。

        增量 = 快照中 extract_msg_idx 之后的新消息，截到「assistant 收尾」的完整前缀
        （未收尾的半轮留给下次），单次上限 MAX_EXTRACT_MESSAGES，超出部分
        下轮补摘；水位只推进到实际喂入的最后一条 index。

        水位失效处理：extract_msg_idx >= len(snapshot) 时（如 Path A 压缩重建列表后），
        回退 0 全量重摘（幂等安全）。

        原子序（安全方向）：先写 SUMMARY.md 成功，再推进内存水位——失败只重复摘要不漏摘要。

        异常闸（LLM调用设计方案.md §5.4）：catch 通用层异常并按 LLMErrorClass
        记因后转「无效结果」，兜底 = 本次跳过（旧摘要与水位均保留），等下次阈值再触发。
        """
        path = get_session_summary_path(user_id, app_id, session_id)
        try:
            start_idx = state.extract_msg_idx
            if start_idx == len(messages_snapshot):
                # 水位恰好对齐列表末尾：无新消息（如快照恢复后），本次无需摘要
                return
            if start_idx > len(messages_snapshot):
                # 水位失效（Path B 压缩重建出更短列表），回退 0 全量重摘
                start_idx = 0
            end_idx = min(start_idx + MAX_EXTRACT_MESSAGES, len(messages_snapshot))
            # 截到 assistant 收尾：本轮未完成的半轮（如停止/异常）留待下次
            consumed_idx = start_idx
            for i in range(start_idx, end_idx):
                if messages_snapshot[i].get("role") == "assistant":
                    consumed_idx = i + 1
            if consumed_idx == start_idx:
                # 无 assistant 收尾的完整轮次，跳过
                return
            fed = messages_snapshot[start_idx:consumed_idx]
            current_notes = self._load_from_path(path)
            # 后台提取独立 root trace：llm span/token 归因真实 app（docs/链路追踪设计.md §5）
            from codegenx.ai_service.trace import background_trace
            async with background_trace(
                "session_summary", session_id=session_id, app_id=app_id, user_id=user_id,
                note=f"摘要提取 {len(fed)} 条消息", trigger_event=trigger,
            ):
                summary = await self._summarize(fed, current_notes, str(path))
            if summary is None:
                # 校验闸：空输出 = 无效结果，旧摘要与水位均保留，等下次阈值触发
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(summary, encoding="utf-8")
            # 摘要写盘成功 → 推进水位到实际喂入的下一条 index（单写者=本后台任务）
            state.extract_msg_idx = consumed_idx
            # 水位随写盘落盘，保证会话恢复时能还原
            self.save_state(
                state, user_id=user_id, app_id=app_id, session_id=session_id
            )
            log.debug(
                "session summary 压缩完成:{} 水位 {} → {}",
                path, start_idx, consumed_idx,
            )
        except Exception as exc:
            err_class = classify_llm_error(exc)
            log.error("session summary 提取异常（{}）", err_class.value)
            log.error(traceback.format_exc())
        finally:
            state.extracting = False
            state.extract_task = None

    @staticmethod
    def _load_from_path(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    async def _summarize(
        self,
        messages: list[dict],
        current_notes: str,
        notes_path: str,
    ) -> str | None:
        """调用大模型更新结构化会话摘要；空输出返回 None（无效结果，水位不推进）。"""
        system_prompt = build_summary_extraction_prompt(
            messages=messages,
            current_notes=current_notes,
            notes_path=notes_path,
        )

        # 走韧性层（摘要模型链 + 熔断/重试/降级）
        updated_notes = await resilient_invoke(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "请根据上述指令更新会话笔记。"},
            ],
            chain=self._model_chain,
            label="summary",
            temperature=0.0,
            max_tokens=4096,
        )

        if not isinstance(updated_notes, dict):
            # 校验闸（句法层）：非 dict 返回 = 无效结果 → 返回 None（旧笔记与水位保留）
            log.warning("[summary] 摘要返回结构异常（{}），保留旧笔记兜底", type(updated_notes).__name__)
            return None
        content = str(updated_notes.get("content") or "").strip()
        if not content:
            # 校验闸（句法层）：空输出 = 无效结果 → 返回 None（旧笔记与水位保留，等下次阈值触发）
            log.warning("[summary] 摘要输出为空，保留旧笔记兜底")
            return None

        return content


def initialize_summary(system_app) -> SessionSummaryService:
    """注册会话摘要服务组件（system_app.initialize_components 调用）。"""
    return system_app.register(SessionSummaryService)
