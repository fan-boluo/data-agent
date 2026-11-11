"""Human-in-the-loop Gate：高风险工具调用在执行前挂起，等待人工审批。

挂点：ToolExecutor.execute 的 BEFORE_TOOL_CALL 钩子之后、工具真正执行之前（闸门，不是事后告警）。
链路：风险策略判定 → 注册 PendingApproval(future) → SSE 推 ApprovalRequired
      → 前端 POST /chat/approval → 恢复 future → 放行 / 拒绝（拒绝原因回填 tool result 让 lead 改路）。
兜底：hitl.timeout_seconds 内无人处理则自动拒绝；用户停止请求时随 stop_signal 终止。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from codegenx.ai_service.agent.agent_schema import AgentEventType, AgentState
from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState, TurnStoppedError
from codegenx.ai_service.utils.config import config
from shared import log

# ── 风险策略常量 ──────────────────────────────────────────────

# 直接写文件/执行命令类工具：一律视为写操作
WRITE_TOOL_NAMES = {"write_file", "edit_file", "delete_file", "run_bash", "bash", "execute_command"}
# 落盘导出类工具
EXPORT_TOOL_NAMES = {"write_file", "edit_file"}

# SQL 写操作/DDL/DCL 关键字（execute_postgresql 自身有只读守卫，这里提前拦给审批人看）
_WRITE_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|RENAME|GRANT|REVOKE|COPY|VACUUM)\b",
    re.IGNORECASE,
)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
# 只取 FROM/JOIN 后的 schema.table，避免把 a.column 误判成跨 schema
_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$]*)\.([A-Za-z_][\w$]*)", re.IGNORECASE)
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}
# run_python 代码里的导出痕迹
_EXPORT_CODE_RE = re.compile(
    r"(to_csv|to_excel|to_parquet|open\s*\([^)]*['\"]w|savetxt|savefig)", re.IGNORECASE
)


@dataclass
class RiskAssessment:
    """一次工具调用的风险判定结果（展示给审批人）。"""

    level: str  # high / medium
    reasons: list[str]
    title: str
    sql: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "title": self.title,
            "sql": self.sql,
            "arguments": self.arguments,
        }


@dataclass
class ApprovalDecision:
    approved: bool
    reason: str = ""
    edited_arguments: dict[str, Any] | None = None


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    request_id: str
    tool_call_id: str
    tool_name: str
    risk: RiskAssessment
    future: asyncio.Future[ApprovalDecision]
    created_at: float = field(default_factory=time.time)


def _current_schema(session_state: RuntimeSessionState | None) -> str:
    current_db = getattr(session_state, "current_db", None) if session_state else None
    if current_db and "." in str(current_db):
        return str(current_db).split(".", 1)[1]
    return ""


def _cross_schema_refs(sql: str, session_state: RuntimeSessionState | None) -> list[str]:
    """找出 SQL 中访问了当前数据集 schema 之外的表引用。

    当前数据集格式为 datasource.schema，datasource 名（如 business）也可能被模型
    写成表限定前缀，一并视为合法，避免误拦。
    """
    current_db = str(getattr(session_state, "current_db", "") or "") if session_state else ""
    allowed = {part.lower() for part in current_db.split(".") if part}
    refs: list[str] = []
    for schema, table in _TABLE_REF_RE.findall(sql):
        lowered = schema.lower()
        if lowered in _SYSTEM_SCHEMAS or lowered in allowed:
            continue
        refs.append(f"{schema}.{table}")
    return refs


def assess_tool_call(
    tool_name: str, arguments: dict[str, Any], session_state: RuntimeSessionState | None = None,
) -> RiskAssessment | None:
    """按 hitl 配置判定风险；None 表示放行（无需审批）。"""
    cfg = config.hitl
    if not cfg.enabled:
        return None

    sql = str(arguments.get("sql") or "").strip()
    code = str(arguments.get("code") or arguments.get("script") or "")
    args_preview = {
        k: (v if not isinstance(v, str) or len(v) <= 2000 else v[:2000] + "...")
        for k, v in arguments.items()
    }

    # 1) 写操作：写文件/执行命令，或 SQL 命中写关键字
    if cfg.require_write:
        if tool_name in WRITE_TOOL_NAMES:
            return RiskAssessment("high", ["写操作"], f"{tool_name}：写文件/执行命令",
                                  arguments=args_preview)
        if sql and _WRITE_SQL_RE.search(sql):
            return RiskAssessment("high", ["写操作"], f"{tool_name}：SQL 含写操作/DDL",
                                  sql=sql, arguments=args_preview)

    # 2) 跨 schema 访问：可能导致越权读取其他数据集
    if cfg.require_cross_schema and sql:
        cross = _cross_schema_refs(sql, session_state)
        if cross:
            return RiskAssessment("high", ["跨 schema 访问"],
                                  f"{tool_name}：访问了当前数据集之外的 schema",
                                  sql=sql, arguments=args_preview)

    # 3) 导出/落盘：文件写入与 python 代码中的导出调用
    if cfg.require_export:
        if tool_name in EXPORT_TOOL_NAMES or (code and _EXPORT_CODE_RE.search(code)):
            return RiskAssessment("medium", ["导出/落盘"], f"{tool_name}：结果写入磁盘",
                                  sql=sql, arguments=args_preview)

    # 4) 大范围扫描：无 WHERE 且无 LIMIT，或 LIMIT 超过行数阈值
    if cfg.require_full_scan and sql:
        limit_match = _LIMIT_RE.search(sql)
        if limit_match and int(limit_match.group(1)) > cfg.max_scan_rows:
            return RiskAssessment("medium", ["扫描行数超阈值"],
                                  f"{tool_name}：LIMIT {limit_match.group(1)} 超过 {cfg.max_scan_rows}",
                                  sql=sql, arguments=args_preview)
        if not _WHERE_RE.search(sql) and not limit_match:
            return RiskAssessment("medium", ["全表扫描"], f"{tool_name}：SQL 无 WHERE 且无 LIMIT",
                                  sql=sql, arguments=args_preview)
    return None


class ApprovalGate:
    """进程内审批闸门：挂起 future + SSE 通知 + 外部 resolve。

    单进程部署（网关与 ai_service 同 app），future 存活于同一事件循环；
    若后续拆进程，需将 resolve 改成跨进程信号（Redis pub/sub）。
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}

    # ── 挂起 ──────────────────────────────────────────────
    async def request(
        self,
        *,
        session_state: RuntimeSessionState,
        tool_call: dict[str, Any],
        risk: RiskAssessment,
    ) -> ApprovalDecision:
        cfg = config.hitl
        if not cfg.enabled or cfg.auto_approve:
            return ApprovalDecision(approved=True, reason="hitl 未启用，自动放行")

        approval_id = uuid.uuid4().hex[:12]
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_state.session_id,
            request_id=session_state.request_id,
            tool_call_id=str(tool_call.get("id") or ""),
            tool_name=str(tool_call.get("name") or ""),
            risk=risk,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[approval_id] = pending
        # 会话侧留痕（可查询/排障；future 本体不外泄）
        session_state.pending_approvals[approval_id] = {
            "tool_name": pending.tool_name,
            "risk": risk.to_payload(),
            "created_at": pending.created_at,
        }
        try:
            await self._publish(session_state, pending, cfg.timeout_seconds)
            log.info("[hitl] 等待审批 approval_id={} tool={} risk={} reasons={}",
                     approval_id, pending.tool_name, risk.level, risk.reasons)
            return await self._wait(pending, session_state, cfg.timeout_seconds)
        finally:
            self._pending.pop(approval_id, None)
            session_state.pending_approvals.pop(approval_id, None)

    async def _wait(
        self, pending: PendingApproval, session_state: RuntimeSessionState, timeout: float,
    ) -> ApprovalDecision:
        stop_task = asyncio.create_task(session_state.stop_signal.wait())
        try:
            done, _ = await asyncio.wait(
                {pending.future, stop_task}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and stop_task.result():
                raise TurnStoppedError(session_state.stop_reason or "user-stop")
            if pending.future in done:
                decision = pending.future.result()
                log.info("[hitl] 收到审批结果 approval_id={} approved={} reason={}",
                         pending.approval_id, decision.approved, decision.reason)
                return decision
            log.warning("[hitl] 审批超时自动拒绝 approval_id={} timeout={}s",
                        pending.approval_id, timeout)
            return ApprovalDecision(
                approved=False, reason=f"审批超时（{timeout:.0f}s）未响应，已自动拒绝",
            )
        finally:
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

    async def _publish(
        self, session_state: RuntimeSessionState, pending: PendingApproval, timeout: float,
    ) -> None:
        from codegenx.ai_service.system_app import get_app

        try:
            await get_app().message_bus.publish_runtime_event(
                session_state.publish_context,
                event_type=AgentEventType.APPROVAL_REQUIRED,
                state=AgentState.RUNNING,
                data={
                    "approval_id": pending.approval_id,
                    "request_id": pending.request_id,
                    "session_id": pending.session_id,
                    "tool_call_id": pending.tool_call_id,
                    "tool_name": pending.tool_name,
                    "timeout_seconds": timeout,
                    **pending.risk.to_payload(),
                },
            )
        except Exception as exc:  # 推不出去也要让前端能通过 GET 查询（记录日志即可）
            log.warning("[hitl] ApprovalRequired 事件推送失败 approval_id={}: {}",
                        pending.approval_id, exc)

    # ── 恢复 ──────────────────────────────────────────────
    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        reason: str = "",
        edited_arguments: dict[str, Any] | None = None,
    ) -> bool:
        pending = self._pending.get(str(approval_id))
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(
            ApprovalDecision(approved=approved, reason=reason, edited_arguments=edited_arguments)
        )
        return True

    def pending_of_request(self, request_id: str) -> list[dict[str, Any]]:
        """某请求下待审批项（前端断线重连后可补拉）。"""
        return [
            {
                "approval_id": p.approval_id,
                "tool_name": p.tool_name,
                "tool_call_id": p.tool_call_id,
                "risk": p.risk.to_payload(),
                "created_at": p.created_at,
            }
            for p in self._pending.values()
            if p.request_id == str(request_id)
        ]


_gate: ApprovalGate | None = None


def get_approval_gate() -> ApprovalGate:
    global _gate
    if _gate is None:
        _gate = ApprovalGate()
    return _gate
