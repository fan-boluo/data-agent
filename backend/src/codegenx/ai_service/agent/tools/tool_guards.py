"""工具调用前守卫（BEFORE_TOOL_CALL 唯一入口）。

执行前检查全部收敛为两个 hook：
  priority 10  tool_permission_guard  规则链（存在性/配额/路径/熔断可用性）→ deny 即 block
  priority 50  hitl_approval_guard    高风险调用挂起等人工审批；拒绝 → block；改参数 → patches

约定：
- block 的 message 由 ToolExecutor 原样回填为 tool result（让 LLM 改路而非直接失败）；
  指标口径由 ctx.data["error_class"] 标注。
- 改写参数走 ctx.patches["tool_arguments"]，由 ToolExecutor 合并后执行。
"""

from __future__ import annotations

from codegenx.ai_service.agent.hitl import assess_tool_call, get_approval_gate
from codegenx.ai_service.agent.tools.permission import PermissionContext, PermissionManager
from codegenx.ai_service.hook import HookContext, HookDecision, HookEvent, on
from shared import log

_permission = PermissionManager()


def _tool_call_of(ctx: HookContext) -> tuple[str, dict]:
    tool_call = ctx.data.get("tool_call") or {}
    return str(tool_call.get("name") or ""), dict(tool_call.get("arguments") or {})


@on(HookEvent.BEFORE_TOOL_CALL, name="tool_permission_guard", priority=10)
async def tool_permission_guard(ctx: HookContext) -> HookDecision | None:
    """许可判定：命中任一规则即阻断（规则顺序见 permission.DEFAULT_RULES）。"""
    tool_name, arguments = _tool_call_of(ctx)
    if not tool_name:
        return None
    decision = _permission.check(PermissionContext(tool_name, arguments, ctx.session))
    if not decision.denied:
        return None
    ctx.data["error_class"] = decision.error_class or "permission_denied"
    log.info("[tool_guard] 阻断 tool={} class={} reason={}",
             tool_name, decision.error_class, decision.reason)
    return HookDecision.block(decision.reason)


@on(HookEvent.BEFORE_TOOL_CALL, name="hitl_approval_guard", priority=50)
async def hitl_approval_guard(ctx: HookContext) -> HookDecision | None:
    """HITL 闸门：高风险调用挂起等审批；拒绝回填原因，审批人改写参数则合并后执行。"""
    session_state = ctx.session
    tool_name, arguments = _tool_call_of(ctx)
    if session_state is None or not tool_name:
        return None
    risk = assess_tool_call(tool_name, arguments, session_state)
    if risk is None:
        return None

    decision = await get_approval_gate().request(
        session_state=session_state,
        tool_call={"id": str((ctx.data.get("tool_call") or {}).get("id") or ""),
                   "name": tool_name, "arguments": arguments},
        risk=risk,
    )
    if not decision.approved:
        ctx.data["error_class"] = "approval_rejected"
        log.info("[hitl] 审批拒绝 tool={} reasons={} reason={}",
                 tool_name, risk.reasons, decision.reason)
        reason = decision.reason or "用户拒绝了该操作。"
        if not reason.endswith(("。", "！", "？", ".", "!", "?")):
            reason += "。"
        return HookDecision.block(
            f"人工审批未通过（风险项：{'、'.join(risk.reasons)}）。"
            f"{reason}"
            "请调整方案（例如缩小扫描范围、改为只读查询、换用其他数据源）后重新执行。"
        )
    # approve_with_edit：把审批人改写后的参数交给 ToolExecutor 合并
    if decision.edited_arguments:
        ctx.patches["tool_arguments"] = dict(decision.edited_arguments)
    return None
