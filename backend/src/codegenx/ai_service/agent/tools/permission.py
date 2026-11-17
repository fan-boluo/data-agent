"""工具调用许可判定（默认模式：规则链，命中 deny 即阻断）。

设计约定：
- 每条规则是纯同步判定（无 IO），返回 None 表示放行，返回 deny 决定则立即阻断；
- 规则按顺序执行：存在性 → 次数配额 → 路径安全 → 熔断可用性；
- 未命中任何规则 = 放行（是否需要人工审批由 HITL 风险策略在 tool_guards 里另行判定）；
- deny 的 reason 会原样回填为 tool result，让 LLM 改路；error_class 用于指标口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from codegenx.ai_service.agent.tools.tool_circuit import get_tool_breaker
from codegenx.ai_service.system_app import get_app
from codegenx.ai_service.utils.config import config
from shared.constants import get_code_dir

if TYPE_CHECKING:
    from codegenx.ai_service.agent.runtime_schema import RuntimeSessionState

# 未知工具名疑似 SQL 的判定前缀（模型常把 SQL 误填进 tool name）
_SQL_LIKE_PREFIXES = ("select", "with", "insert", "update", "delete", "explain", "show", "desc")
# 路径类参数（文件操作工具的参数名可能叫 path/filename/src/dest）
_PATH_KEYS = ("path", "filename", "src", "dest", "file_path")


@dataclass
class PermissionContext:
    """一次工具调用的判定上下文。"""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    session: "RuntimeSessionState | None" = None

    @property
    def user_id(self) -> str:
        return str(getattr(self.session, "user_id", "") or "") if self.session else ""

    @property
    def app_id(self) -> str:
        request = getattr(self.session, "request", None) if self.session else None
        app_id = str(getattr(request, "app_id", "") or "") if request else ""
        return app_id or "main"


@dataclass
class PermissionDecision:
    behavior: str = "allow"  # allow / deny
    reason: str = ""
    error_class: str = ""

    @property
    def denied(self) -> bool:
        return self.behavior == "deny"


Rule = Callable[[PermissionContext], PermissionDecision | None]


def _deny(reason: str, error_class: str) -> PermissionDecision:
    return PermissionDecision("deny", reason, error_class)


# ── 规则实现 ──────────────────────────────────────────────

def _agent_tool_names(session: "RuntimeSessionState | None") -> list[str]:
    """当前智能体可用工具名：优先 agent.tools 白名单，空则回退全局注册表。"""
    try:
        agent = config.get_agent(getattr(session, "agent_name", "") or None)
        names = [str(n).strip() for n in (agent.tools or []) if str(n).strip()]
    except Exception:
        names = []
    if names:
        return names
    return [t.name for t in get_app().tool_registry.tools]


def rule_tool_exists(ctx: PermissionContext) -> PermissionDecision | None:
    """工具必须存在于注册表；不存在时给出可用工具清单（含 SQL 误填纠正提示）。"""
    if any(t.name == ctx.tool_name for t in get_app().tool_registry.tools):
        return None
    bad_name = (ctx.tool_name or "").strip().splitlines()[0][:40] or "unknown"
    hint = ""
    if bad_name.lower().startswith(_SQL_LIKE_PREFIXES):
        hint = "（你把 SQL 语句填到了工具名字段。SQL 应作为 execute_postgresql 的 sql 参数传入。）"
    return _deny(
        f"工具名「{bad_name}」不存在，本次调用未执行。{hint}"
        f"可用工具：{', '.join(_agent_tool_names(ctx.session))}。"
        "请按工具定义重新发起正确调用。",
        "arg_invalid",
    )


def rule_call_quota(ctx: PermissionContext) -> PermissionDecision | None:
    """同工具单请求调用次数上限（0=关闭），防 LLM 死循环重复调用。"""
    limit = int(config.tools.max_calls_per_tool or 0)
    if limit <= 0 or ctx.session is None:
        return None
    counts = ctx.session.activate_turn.tool_call_counts  # 按 turn 计数（每个请求重建）
    counts[ctx.tool_name] = int(counts.get(ctx.tool_name, 0)) + 1
    if counts[ctx.tool_name] <= limit:
        return None
    return _deny(
        f"工具 {ctx.tool_name} 在本轮请求中已调用 {counts[ctx.tool_name] - 1} 次，超过上限 {limit} 次。"
        "请基于已有结果继续推进或直接作答，不要重复调用相同的工具。",
        "quota_exceeded",
    )


def _safe_paths(session: "RuntimeSessionState | None") -> list[Path]:
    context = getattr(session, "context", None) if session is not None else None
    if not context:
        return []
    return [Path(path).resolve() for path in context.metadata.safety_path]


def _resolve_candidate(value: str, app_id: str, user_id: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = get_code_dir(user_id or "main", app_id) / candidate
    return candidate.resolve()


def rule_path_safety(ctx: PermissionContext) -> PermissionDecision | None:
    """路径安全：禁止 .. 穿越，且目标必须落在会话 safe_path 内。"""
    for key in _PATH_KEYS:
        value = ctx.tool_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if "../" in value or "..\\" in value:
            return _deny(f"path 参数 {key} 含路径穿越（../），已拒绝", "permission_denied")
        try:
            target = _resolve_candidate(value, ctx.app_id, ctx.user_id)
        except Exception as exc:
            return _deny(f"{ctx.tool_name}：路径解析错误：{exc}", "permission_denied")
        safe_paths = _safe_paths(ctx.session)
        if not any(_is_within(target, sp) for sp in safe_paths):
            return _deny(
                f"{ctx.tool_name}：越界访问：目标路径 '{target}' 不在 safe_path 允许范围内",
                "permission_denied",
            )
    return None


def _is_within(target: Path, base: Path) -> bool:
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def rule_tool_available(ctx: PermissionContext) -> PermissionDecision | None:
    """熔断可用性：打开且仍在冷却期时快速失败。

    只读判断，不触发状态迁移；冷却结束后的半开探测名额仍由 ToolExecutor.acquire 控制。
    """
    if get_tool_breaker(ctx.tool_name).is_open():
        return _deny(f"工具 {ctx.tool_name} 暂时不可用（熔断打开），请稍后重试或改用其他工具",
                     "circuit_open")
    return None


DEFAULT_RULES: tuple[Rule, ...] = (
    rule_tool_exists,
    rule_call_quota,
    rule_path_safety,
    rule_tool_available,
)


class PermissionManager:
    """默认模式下按规则链判定：命中 deny 立即阻断，否则放行。"""

    def __init__(self, rules: Sequence[Rule] | None = None) -> None:
        self._rules: tuple[Rule, ...] = tuple(rules or DEFAULT_RULES)

    def check(self, ctx: PermissionContext) -> PermissionDecision:
        for rule in self._rules:
            decision = rule(ctx)
            if decision is not None:
                return decision
        return PermissionDecision()

