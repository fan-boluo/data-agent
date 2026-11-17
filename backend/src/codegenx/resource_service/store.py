"""平台资源与项目授权读写（PostgreSQL agent schema）。

表结构真源：init/params_schema.sql
- agent.tool / agent.skill：平台资源，is_platform_enabled 为全局总开关（紧急熔断）
- agent.app_tool / agent.app_skill：按 app 的授权记录（开关 + 覆写配置）
- agent.skill_tool_rel：skill 内包含的 tool 关联，只读展示（由 skill 定义决定，不做增删）
"""
from __future__ import annotations

import json
from typing import Any

from db.postgres.postgres_client import execute, fetch, fetchrow

from shared import log

# agent.* 表位于应用元数据库（主连接池），与 telemetry/semantic 同实例


def _loads_json(value: Any) -> dict:
    """asyncpg 未注册 jsonb 编解码器时返回字符串，这里统一还原为 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


async def list_tools() -> list[dict]:
    rows = await fetch(
        "SELECT tool_code, tool_name, description, is_platform_enabled, remark, operator_id, updated_at "
        "FROM agent.tool ORDER BY id"
    )
    rels = await fetch("SELECT skill_code, tool_code FROM agent.skill_tool_rel")
    skill_map: dict[str, list[str]] = {}
    for rel in rels:
        skill_map.setdefault(str(rel["tool_code"]), []).append(str(rel["skill_code"]))
    return [
        {
            "tool_code": str(row["tool_code"]),
            "tool_name": row["tool_name"],
            "description": row["description"],
            "is_platform_enabled": bool(row["is_platform_enabled"]),
            "remark": row["remark"],
            "operator_id": row["operator_id"],
            "skill_codes": sorted(skill_map.get(str(row["tool_code"]), [])),
            "update_time": row["updated_at"],
        }
        for row in rows
    ]


async def list_skills() -> list[dict]:
    rows = await fetch(
        "SELECT skill_code, skill_name, description, is_platform_enabled, remark, operator_id, updated_at "
        "FROM agent.skill ORDER BY id"
    )
    rels = await fetch("SELECT skill_code, tool_code FROM agent.skill_tool_rel")
    tool_map: dict[str, list[str]] = {}
    for rel in rels:
        tool_map.setdefault(str(rel["skill_code"]), []).append(str(rel["tool_code"]))
    return [
        {
            "skill_code": str(row["skill_code"]),
            "skill_name": row["skill_name"],
            "description": row["description"],
            "is_platform_enabled": bool(row["is_platform_enabled"]),
            "remark": row["remark"],
            "operator_id": row["operator_id"],
            "tool_codes": sorted(tool_map.get(str(row["skill_code"]), [])),
            "update_time": row["updated_at"],
        }
        for row in rows
    ]


async def update_tool(
    tool_code: str, is_platform_enabled: bool, remark: str | None, operator_id: str | None
) -> bool:
    """平台工具总开关 + 备注 + 最后操作人（全局熔断只改这里，不影响各 app 已保存的授权记录）。"""
    result = await execute(
        "UPDATE agent.tool SET is_platform_enabled = $2, remark = $3, operator_id = $4, updated_at = now() "
        "WHERE tool_code = $1",
        tool_code, is_platform_enabled, remark, operator_id,
    )
    return result.endswith("1")


async def update_skill(
    skill_code: str, is_platform_enabled: bool, remark: str | None, operator_id: str | None
) -> bool:
    result = await execute(
        "UPDATE agent.skill SET is_platform_enabled = $2, remark = $3, operator_id = $4, updated_at = now() "
        "WHERE skill_code = $1",
        skill_code, is_platform_enabled, remark, operator_id,
    )
    return result.endswith("1")


async def list_app_tools(app_id: str) -> list[dict]:
    """app 下工具授权：左连平台工具全集。

    未建授权记录时默认继承平台状态（平台开放即默认授权），平台下线一律视为不可用。
    """
    rows = await fetch(
        "SELECT t.tool_code, t.tool_name, t.description, t.is_platform_enabled, "
        "       t.default_config, "
        "       (COALESCE(a.is_enabled, true) AND t.is_platform_enabled) AS is_enabled, "
        "       COALESCE(a.override_config, '{}'::jsonb) AS override_config, "
        "       a.remark, a.operator_id, a.updated_at "
        "FROM agent.tool t "
        "LEFT JOIN agent.app_tool a ON a.tool_code = t.tool_code AND a.app_id = $1 "
        "ORDER BY t.id",
        app_id,
    )
    return [
        {
            "tool_code": str(row["tool_code"]),
            "tool_name": row["tool_name"],
            "description": row["description"],
            "is_platform_enabled": bool(row["is_platform_enabled"]),
            "is_enabled": bool(row["is_enabled"]),
            "default_config": _loads_json(row["default_config"]),
            "override_config": _loads_json(row["override_config"]),
            "remark": row["remark"],
            "operator_id": row["operator_id"],
            "update_time": row["updated_at"],
        }
        for row in rows
    ]


async def list_app_skills(app_id: str) -> list[dict]:
    rows = await fetch(
        "SELECT s.skill_code, s.skill_name, s.description, s.is_platform_enabled, "
        "       (COALESCE(a.is_enabled, true) AND s.is_platform_enabled) AS is_enabled, "
        "       a.remark, a.operator_id, a.updated_at "
        "FROM agent.skill s "
        "LEFT JOIN agent.app_skill a ON a.skill_code = s.skill_code AND a.app_id = $1 "
        "ORDER BY s.id",
        app_id,
    )
    return [
        {
            "skill_code": str(row["skill_code"]),
            "skill_name": row["skill_name"],
            "description": row["description"],
            "is_platform_enabled": bool(row["is_platform_enabled"]),
            "is_enabled": bool(row["is_enabled"]),
            "remark": row["remark"],
            "operator_id": row["operator_id"],
            "update_time": row["updated_at"],
        }
        for row in rows
    ]


async def get_tool_platform_enabled(tool_code: str) -> bool | None:
    """工具是否存在 + 平台是否开放；不存在返回 None。"""
    value = await fetchrow(
        "SELECT is_platform_enabled FROM agent.tool WHERE tool_code = $1", tool_code
    )
    return None if value is None else bool(value["is_platform_enabled"])


async def get_skill_platform_enabled(skill_code: str) -> bool | None:
    value = await fetchrow(
        "SELECT is_platform_enabled FROM agent.skill WHERE skill_code = $1", skill_code
    )
    return None if value is None else bool(value["is_platform_enabled"])


async def upsert_app_tool(
    app_id: str,
    tool_code: str,
    is_enabled: bool,
    override_config: dict | None,
    remark: str | None,
    operator_id: str | None,
) -> None:
    await execute(
        "INSERT INTO agent.app_tool (app_id, tool_code, is_enabled, override_config, remark, operator_id) "
        "VALUES ($1, $2, $3, $4::jsonb, $5, $6) "
        "ON CONFLICT (app_id, tool_code) DO UPDATE SET "
        "  is_enabled = EXCLUDED.is_enabled, "
        "  override_config = EXCLUDED.override_config, "
        "  remark = EXCLUDED.remark, "
        "  operator_id = EXCLUDED.operator_id, "
        "  updated_at = now()",
        app_id, tool_code, is_enabled, json.dumps(override_config or {}, ensure_ascii=False), remark,
        operator_id,
    )


async def upsert_app_skill(
    app_id: str,
    skill_code: str,
    is_enabled: bool,
    remark: str | None,
    operator_id: str | None,
) -> None:
    await execute(
        "INSERT INTO agent.app_skill (app_id, skill_code, is_enabled, remark, operator_id) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (app_id, skill_code) DO UPDATE SET "
        "  is_enabled = EXCLUDED.is_enabled, "
        "  remark = EXCLUDED.remark, "
        "  operator_id = EXCLUDED.operator_id, "
        "  updated_at = now()",
        app_id, skill_code, is_enabled, remark, operator_id,
    )


async def init_app_resources(app_id: str) -> None:
    """项目创建时复制平台已开放资源的默认授权（幂等，已存在不覆盖）。"""
    await execute(
        "INSERT INTO agent.app_tool (app_id, tool_code, is_enabled) "
        "SELECT $1, tool_code, true FROM agent.tool WHERE is_platform_enabled = true "
        "ON CONFLICT (app_id, tool_code) DO NOTHING",
        app_id,
    )
    await execute(
        "INSERT INTO agent.app_skill (app_id, skill_code, is_enabled) "
        "SELECT $1, skill_code, true FROM agent.skill WHERE is_platform_enabled = true "
        "ON CONFLICT (app_id, skill_code) DO NOTHING",
        app_id,
    )


# ---------------------- 运行时可用的资源范围（agent 每次执行前读取） ----------------------

async def get_disabled_tool_codes(app_id: str) -> set[str] | None:
    """返回该 app 运行时不可用的工具编码。

    规则：平台总开关关闭 或 项目显式关闭 → 不可用；未建项目授权记录默认继承平台（开放）。
    查库失败（表未初始化/DB 不可用）返回 None，调用方降级为不额外过滤。
    """
    try:
        rows = await fetch(
            "SELECT t.tool_code, t.is_platform_enabled, a.is_enabled AS app_enabled "
            "FROM agent.tool t "
            "LEFT JOIN agent.app_tool a ON a.tool_code = t.tool_code AND a.app_id = $1",
            app_id,
        )
    except Exception as exc:
        log.debug("resource-service load app tool availability failed appId={} error={}", app_id, exc)
        return None
    return {
        str(row["tool_code"])
        for row in rows
        if not row["is_platform_enabled"] or row["app_enabled"] is False
    }


async def get_disabled_skill_codes(app_id: str) -> set[str] | None:
    """返回该 app 运行时不可用的技能编码；规则同工具。"""
    try:
        rows = await fetch(
            "SELECT s.skill_code, s.is_platform_enabled, a.is_enabled AS app_enabled "
            "FROM agent.skill s "
            "LEFT JOIN agent.app_skill a ON a.skill_code = s.skill_code AND a.app_id = $1",
            app_id,
        )
    except Exception as exc:
        log.debug("resource-service load app skill availability failed appId={} error={}", app_id, exc)
        return None
    return {
        str(row["skill_code"])
        for row in rows
        if not row["is_platform_enabled"] or row["app_enabled"] is False
    }


# ---------------------- 平台智能体（全局手动开关；LLM 可用性见 agents.py 实时计算） ----------------------

async def list_agent_rows() -> dict[str, dict] | None:
    """agent.agent 全量按 agent_code 索引；表未初始化/DB 不可用返回 None。"""
    try:
        rows = await fetch(
            "SELECT agent_code, agent_name, description, is_enabled, remark, operator_id, updated_at "
            "FROM agent.agent ORDER BY id"
        )
    except Exception as exc:
        log.debug("resource-service load agent rows failed error={}", exc)
        return None
    return {str(row["agent_code"]): dict(row) for row in rows}


async def upsert_agent(
    agent_code: str,
    agent_name: str,
    description: str | None,
    is_enabled: bool,
    remark: str | None,
    operator_id: str | None,
) -> None:
    """手动总开关 + 备注 upsert（未落库的智能体首次操作时补建行）。"""
    await execute(
        "INSERT INTO agent.agent (agent_code, agent_name, description, is_enabled, remark, operator_id) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (agent_code) DO UPDATE SET "
        "  is_enabled = EXCLUDED.is_enabled, "
        "  remark = EXCLUDED.remark, "
        "  operator_id = EXCLUDED.operator_id, "
        "  updated_at = now()",
        agent_code, agent_name, description, is_enabled, remark, operator_id,
    )


async def get_disabled_agent_codes() -> set[str] | None:
    """手动全局停用的智能体编码；查库失败返回 None（降级为不做手动开关过滤）。"""
    try:
        rows = await fetch("SELECT agent_code FROM agent.agent WHERE is_enabled = false")
    except Exception as exc:
        log.debug("resource-service load disabled agents failed error={}", exc)
        return None
    return {str(row["agent_code"]) for row in rows}
