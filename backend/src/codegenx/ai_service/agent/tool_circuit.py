"""工具 / 数据源级熔断（P0-2）。

复用 LLM 的三态熔断器（CircuitBreaker），按 tool_name 独立计数：
- 统计口径：只累计基础设施类失败（异常、超时、连接/资源类错误）；
- 业务失败：SQL 语法、参数错误等只上报指标与日志，不计入熔断，
  避免用户连续写错 SQL 把工具打成不可用；
- 键为工具名，工具故障全局生效（与请求/应用无关）；
- 指标：codegenx_tool_circuit_state{tool_name} / codegenx_tool_breaker_opens_total{tool_name}。
"""
from __future__ import annotations

import asyncio
from typing import Dict

from shared import log

from codegenx.ai_service.llm.resilience import CircuitBreaker
from codegenx.ai_service.utils.config import config

# 工具通常把底层异常转成 ToolResult.message，这里按文本特征兜底识别基础设施故障。
# 之所以不做成异常类型判断：pymysql/httpx 等异常在各工具内部已被捕获并降级为字符串。
_INFRA_HINTS = (
    # 英文异常信息（pymysql / asyncpg / httpx 等）
    "timeout",
    "timed out",
    "connection",
    "connect ",
    "refused",
    "unreachable",
    "too many connections",
    "deadlock",
    "lock wait",
    "server has gone away",
    "broken pipe",
    "unavailable",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    # 工具在中文提示里包装过的同类故障
    "超时",
    "连接失败",
    "连接超时",
    "无法连接",
    "连接被拒绝",
    "拒绝连接",
    "通信失败",
)

_tool_breakers: Dict[str, CircuitBreaker] = {}


def get_tool_breaker(tool_name: str) -> CircuitBreaker:
    """取（或创建）工具级熔断器；创建即上报状态，便于预热后直接观测。"""
    breaker = _tool_breakers.get(tool_name)
    if breaker is None:
        cfg = config.tools.circuit
        breaker = CircuitBreaker(
            tool_name,
            failure_threshold=cfg.failure_threshold,
            recovery_timeout=cfg.recovery_timeout_seconds,
            half_open_max_calls=cfg.half_open_max_calls,
            half_open_success_rate=cfg.half_open_success_rate,
            metric_prefix="tool",
            metric_label="tool_name",
            log_tag="tool_breaker",
        )
        _tool_breakers[tool_name] = breaker
        log.info("[tool_breaker] {} 已创建 state={}", tool_name, breaker.state)
    return breaker


def is_infra_failure(exc: BaseException | None = None, message: str = "") -> bool:
    """判定是否属于基础设施类失败（计入熔断）。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    text = f"{type(exc).__name__ if exc else ''} {exc or ''} {message}".lower()
    return any(hint in text for hint in _INFRA_HINTS)
