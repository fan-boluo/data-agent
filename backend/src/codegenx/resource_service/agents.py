"""平台智能体管理 + 运行时可用性判定（只做全局层，无 app 级授权）。

实际可用 = agent.agent.is_enabled（管理员手动总开关） ∧ 模型链存在未熔断模型。
LLM 可用性实时读取各模型 CircuitBreaker（is_open=打开且冷却期内），不落库；
DB 不可用时手动开关降级为全启用，避免配置表问题拖垮运行时。
"""
from __future__ import annotations

from codegenx.ai_service.llm.resilience import get_breaker
from codegenx.resource_service import store


def _model_chain(spec) -> list[str]:
    """从规格的类属性 agent_config 取模型链（主模型 + fallback）。"""
    cfg = getattr(spec.cls, "agent_config", None) if spec is not None and spec.cls else None
    return [
        str(m).strip()
        for m in (getattr(cfg, "resolved_model_name_chain", None) or [])
        if str(m or "").strip()
    ]


def _llm_availability(chain: list[str]) -> tuple[bool, str, list[dict]]:
    """链上任一模型熔断器未打开即可用；返回 (可用, 不可用原因, 各模型状态)。"""
    if not chain:
        return False, "未配置模型链", []
    models = []
    for model in chain:
        try:
            breaker = get_breaker(model)
            is_open = breaker.is_open()
            # open 但冷却已结束：下一次调用会转半开探测，视为可恢复可用
            state = "open" if is_open else ("half_open" if breaker.state == "open" else breaker.state)
        except Exception:
            # 熔断器自身异常不当成模型不可用（fail open）
            is_open, state = False, "unknown"
        models.append({"model": model, "state": state, "available": not is_open})
    available = any(m["available"] for m in models)
    return available, ("" if available else "模型链全部熔断打开，等待冷却恢复"), models


def _all_specs() -> list:
    from codegenx.ai_service.system_app import get_app

    return get_app().agents.all_specs()


async def list_agents() -> list[dict]:
    """平台资源页：注册表规格 + DB 手动开关 + 实时 LLM 可用性（每次进入页面即重算）。"""
    specs = _all_specs()
    db_map = await store.list_agent_rows() or {}
    result = []
    for spec in specs:
        row = db_map.get(spec.name) or {}
        available, reason, models = _llm_availability(_model_chain(spec))
        result.append({
            "agent_code": spec.name,
            "agent_name": row.get("agent_name") or spec.name,
            "role": spec.role,
            "description": row.get("description") or spec.desc,
            "is_enabled": bool(row.get("is_enabled", True)),  # 未落库默认启用
            "is_available": available,
            "unavailable_reason": reason,
            "models": models,
            "remark": row.get("remark"),
            "operator_id": row.get("operator_id"),
            "update_time": row.get("updated_at"),
        })
    return result


async def update_agent(
    agent_code: str, is_enabled: bool, remark: str | None, operator_id: str | None
) -> bool:
    """更新手动总开关 + 备注；agent_code 未注册返回 False。"""
    specs = {spec.name: spec for spec in _all_specs()}
    spec = specs.get(agent_code)
    if spec is None:
        return False
    existing = (await store.list_agent_rows() or {}).get(agent_code) or {}
    await store.upsert_agent(
        agent_code=agent_code,
        agent_name=str(existing.get("agent_name") or spec.name),
        description=existing.get("description") or spec.desc,
        is_enabled=is_enabled,
        remark=remark,
        operator_id=operator_id,
    )
    return True


async def get_unavailable_agent_names() -> dict[str, str]:
    """运行时可用性快照：agent name → 不可用原因；可用的不在结果中。

    供 leader 构建可派发清单、dispatch_agent 执行前拦截使用，每次调用实时计算。
    """
    try:
        specs = _all_specs()
    except Exception:
        return {}  # 注册表未初始化（独立调试等）：不做可用性过滤
    disabled = await store.get_disabled_agent_codes() or set()
    unavailable: dict[str, str] = {}
    for spec in specs:
        if spec.name in disabled:
            unavailable[spec.name] = "已被管理员全局停用"
            continue
        available, reason, _ = _llm_availability(_model_chain(spec))
        if not available:
            unavailable[spec.name] = reason
    return unavailable
