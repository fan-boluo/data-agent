"""工具调用记录：结构化入参/结果独立落表（telemetry.tool_semantics）。

通道分工（docs/链路追踪设计.md）：trace span 只记执行骨架（谁在何时调了什么、
多久、成败），本表在 AFTER_TOOL_CALL 后记录**所有工具**的入参/结果
（tool_args/tool_result）：语义工具做结构化提取，其余工具记录通用载荷；
超大入参字段（如 write_file content）先落盘会话目录、表内只存引用+预览；
经 tool_call_id 与 trace_spans.tool_call_id join（评测数据长期保留，
trace_spans 受 retention 清理）。eval 脚本从本表取 SQL 尝试时序。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from shared import log

from codegenx.ai_service.hook import HookContext, HookEvent, on
from codegenx.ai_service.utils.config import config

# execute_postgresql 输出首行提取行数
_ROW_COUNT_RE = re.compile(r"共\s*(\d+)\s*行")
# 结构化入参工具：sql 等字段被评测消费，不参与大字段落盘
_STRUCTURED_ARG_TOOLS = {"execute_postgresql"}


def _generic_semantics(arguments: dict, result: Any) -> tuple[dict, dict]:
    """通用工具：原样记录入参，结果保留 data/message 摘要（落库前统一截断）。"""
    tool_result: dict[str, Any] = {}
    data = getattr(result, "data", None)
    if data not in (None, ""):
        tool_result["data"] = data
    message = getattr(result, "message", None)
    if message:
        tool_result["message"] = str(message)
    return dict(arguments or {}), tool_result


def _extract_semantics(name: str, arguments: dict, result: Any) -> tuple[dict, dict]:
    """按工具提取 (入参, 结果)：语义工具做结构化提取，其余工具走通用提取。"""
    extras = getattr(result, "extras", None) or {}
    if name == "execute_postgresql":
        tool_args: dict[str, Any] = {}
        sql = str(arguments.get("sql") or "")
        if sql:
            tool_args["sql"] = sql
        datasource = arguments.get("datasource") or arguments.get("db_name")
        if datasource:
            tool_args["datasource"] = str(datasource)
        tool_result: dict[str, Any] = {}
        data = str(getattr(result, "data", "") or "")
        match = _ROW_COUNT_RE.search(data)
        if match:
            tool_result["row_count"] = int(match.group(1))
        preview = extras.get("rows_preview")
        if isinstance(preview, list) and preview:
            tool_result["rows_preview"] = preview
        if tool_args or tool_result:
            return tool_args, tool_result
        return _generic_semantics(arguments, result)
    # 语义层召回结构化：search_tables 召回的表名（评测归因"SQL 错因召回缺表"）
    if name == "search_tables" and isinstance(extras.get("tables"), list):
        return {}, {"recalled_tables": [str(t) for t in extras["tables"]]}
    # 其余工具：全部落表，避免统计口径只覆盖语义工具
    return _generic_semantics(arguments, result)


def _spill_large_args(tool_args: dict[str, Any], tool_call_id: str, session: Any) -> dict[str, Any]:
    """入参大字段落盘：字符串超过 tools.persist_threshold 时写会话目录，

    JSONB 只保留引用 + 预览（复用工具大结果落盘的 persist_large_output 机制），
    典型场景是 write_file/edit_file 的 content 字段。
    """
    threshold = config.tools.persist_threshold
    if not any(len(v) > threshold for v in tool_args.values() if isinstance(v, str)):
        return tool_args
    # 延迟导入：避免 hook 模块加载期拉起 compact 包
    from codegenx.ai_service.compact.large_output import persist_large_output

    user_id = str(getattr(session, "user_id", "") or "")
    app_id = str(getattr(session, "app_id", "") or "")
    session_id = str(getattr(session, "session_id", "") or "")
    return {
        key: (
            persist_large_output({"id": f"{tool_call_id}-{key}"}, value, user_id, app_id, session_id)
            if isinstance(value, str) and len(value) > threshold
            else value
        )
        for key, value in tool_args.items()
    }


@on(HookEvent.AFTER_TOOL_CALL, name="tool_semantics_recorder", priority=50)
async def tool_semantics_recorder(ctx: HookContext) -> None:
    """AFTER_TOOL_CALL：工具入参/结果写 telemetry.tool_semantics（TraceStore 批量落库）。

    观测监听器绝不抛错；trace 禁用或无 trace 身份时跳过。
    """
    try:
        from codegenx.ai_service.trace import recorder

        if not recorder._cfg().enabled:
            return
        tc = ctx.data.get("tool_call") or {}
        name = str(tc.get("name") or "")
        arguments = tc.get("arguments") or {}
        if not name or not isinstance(arguments, dict):
            return
        session = ctx.session
        trace_id = str(getattr(session, "trace_id", "") or getattr(session, "request_id", "") or "")
        if not trace_id:
            return
        result = ctx.data.get("result")
        # tool_call_id = LLM 调用 id（与 span/tool 消息配对）；非 LLM 发起时 uuid 兜底
        tool_call_id = str(tc.get("id") or "") or f"call-{uuid.uuid4().hex}"
        tool_args, tool_result = _extract_semantics(name, arguments, result)
        # 大入参先落盘（write_file content 等），再按 trace 配置截断兜底
        if name not in _STRUCTURED_ARG_TOOLS:
            tool_args = _spill_large_args(tool_args, tool_call_id, session)
        # 载荷截断与 trace 同配置：rows_preview 限行、单字段字符上限
        if isinstance(tool_result.get("rows_preview"), list):
            tool_result["rows_preview"] = tool_result["rows_preview"][: recorder._cfg().rows_preview_limit]
        tool_args, _ = recorder._clip(tool_args, recorder._cfg().payload_max_chars)
        tool_result, _ = recorder._clip(tool_result, recorder._cfg().payload_max_chars)
        recorder.store.enqueue("tool_semantics", {
            "tool_call_id": tool_call_id,
            "trace_id": trace_id,
            "span_id": ctx.data.get("span_id"),
            "tool_name": name,
            "success": bool(getattr(result, "success", False)),
            "tool_args": tool_args,
            "tool_result": tool_result,
            "created_at": recorder._utc(),
        })
    except Exception as exc:  # noqa: BLE001 — 观测失败只降级
        log.warning("tool_semantics 记录失败: {}", exc)
