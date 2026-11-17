"""评测运行存储层：eval_runs / eval_run_items 生命周期 + 6 维指标汇总。

设计要点：
  1. 题集只读复用 telemetry.eval_datasets / eval_cases（CLI import-cases 维护）；
  2. 一次 run 的生命周期：pending → running → completed/failed，
     进度用 case_done / case_failed 原子自增，前端轮询即可；
  3. 指标口径与 ai_service/eval/eval.py 一致：EX / VES / valid_sql 取逐题均值，
     另从 trace 派生工具成功率、平均步数、平均耗时、上下文占用。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from db.postgres.postgres_client import execute, fetch, fetchrow

RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_FAILED = "failed"

# 上下文占用超过该比例视为"需要压缩"（与 trace 侧 breach 阈值 0.8 对齐）
CONTEXT_BREACH_RATIO = 0.8


def _dict(row) -> dict | None:
    """asyncpg.Record → dict（JSONB 字段统一反序列化为对象）。"""
    if row is None:
        return None
    out = dict(row)
    if isinstance(out.get("metrics"), str):
        out["metrics"] = json.loads(out["metrics"] or "{}")
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _avg(values: list[float]) -> float | None:
    """均值（无样本返回 None，避免前端把"没数据"显示成 0）。"""
    return round(sum(values) / len(values), 4) if values else None


# ============================================================ 题集读取

async def list_datasets() -> list[dict]:
    """可选评测题集（含题数），供前端下拉与 run 创建时选择。"""
    rows = await fetch(
        """
        SELECT d.id, d.name, d.description, d.created_at,
               COUNT(c.id) AS case_total,
               COUNT(c.id) FILTER (WHERE c.status='verified') AS case_verified
        FROM telemetry.eval_datasets d
        LEFT JOIN telemetry.eval_cases c ON c.dataset_id = d.id
        GROUP BY d.id ORDER BY d.id
        """)
    return [dict(r) for r in rows]


async def get_dataset(dataset_id: int | None = None, name: str | None = None) -> dict | None:
    """按 id 或 name 取题集（二选一）。"""
    if dataset_id:
        return _dict(await fetchrow("SELECT * FROM telemetry.eval_datasets WHERE id=$1", dataset_id))
    if name:
        return _dict(await fetchrow("SELECT * FROM telemetry.eval_datasets WHERE name=$1", name))
    return None


async def list_cases(dataset_id: int, limit: int = 0) -> list[dict]:
    """取题集的 verified 题目（评测只跑已校验且带 gold_sql 的题），limit=0 为全量。"""
    sql = """
        SELECT * FROM telemetry.eval_cases
        WHERE dataset_id=$1 AND status='verified' AND gold_sql <> ''
        ORDER BY id
    """
    if limit and limit > 0:
        return [_dict(r) for r in await fetch(sql + " LIMIT $2", dataset_id, limit)]
    return [_dict(r) for r in await fetch(sql, dataset_id)]


# ============================================================ run 生命周期

async def create_run(
    dataset_id: int,
    name: str = "",
    baseline_run_id: int | None = None,
    limit_count: int = 0,
    source: str = "api",
) -> dict:
    """建 run（pending）。name 缺省为 数据集名-时间戳。"""
    ds = await get_dataset(dataset_id)
    run_name = name or f"{(ds or {}).get('name', 'eval')}-{_utcnow():%Y%m%d-%H%M%S}"
    row = await fetchrow(
        """
        INSERT INTO telemetry.eval_runs (dataset_id, name, baseline_run_id, limit_count, source)
        VALUES ($1,$2,$3,$4,$5) RETURNING *
        """, dataset_id, run_name, baseline_run_id, limit_count, source)
    return _dict(row)


async def get_run(run_id: int) -> dict | None:
    return _dict(await fetchrow("SELECT * FROM telemetry.eval_runs WHERE id=$1", run_id))


async def list_runs(dataset_id: int | None = None, limit: int = 20) -> list[dict]:
    """历史 run 列表（按创建时间倒序），用于榜单/对比入口。"""
    if dataset_id:
        rows = await fetch(
            "SELECT * FROM telemetry.eval_runs WHERE dataset_id=$1 ORDER BY id DESC LIMIT $2",
            dataset_id, limit)
    else:
        rows = await fetch("SELECT * FROM telemetry.eval_runs ORDER BY id DESC LIMIT $1", limit)
    return [_dict(r) for r in rows]


async def set_run(run_id: int, **fields) -> None:
    """更新 run 字段（status/metrics/report_md/error 等），自动刷新 updated_at。"""
    allowed = {
        "status", "case_total", "case_done", "case_failed", "metrics",
        "report_md", "error", "started_at", "ended_at",
    }
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "metrics" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        args.append(value)
        sets.append(f"{key}=${len(args)}")
    if not sets:
        return
    args.append(run_id)
    await execute(
        f"UPDATE telemetry.eval_runs SET {', '.join(sets)}, updated_at=now() WHERE id=${len(args)}",
        *args)


async def start_run(run_id: int, case_total: int) -> None:
    await set_run(run_id, status=RUN_STATUS_RUNNING, case_total=case_total,
                  case_done=0, case_failed=0, started_at=_utcnow(), error=None)


async def bump_progress(run_id: int, done: int = 0, failed: int = 0) -> None:
    """进度自增（并发场景下用 SQL 自增，避免读改写丢更新）。"""
    await execute(
        """
        UPDATE telemetry.eval_runs
        SET case_done = case_done + $2, case_failed = case_failed + $3, updated_at=now()
        WHERE id=$1
        """, run_id, done, failed)


# ============================================================ 逐题结果

async def ensure_items(run_id: int, case_ids: list[int]) -> None:
    """为本次要跑的题建 pending 行（重复触发幂等）。"""
    if not case_ids:
        return
    await execute(
        """
        INSERT INTO telemetry.eval_run_items (run_id, case_id)
        SELECT $1, unnest($2::bigint[])
        ON CONFLICT (run_id, case_id) DO NOTHING
        """, run_id, case_ids)


async def update_item(run_id: int, case_id: int, **fields) -> None:
    """更新单题结果（status/trace_id/评分/派生指标/error）。"""
    allowed = {
        "trace_id", "status", "ex", "ves", "valid_sql", "category", "detail",
        "steps", "tool_calls", "tool_failures", "context_usage", "duration_ms", "error",
    }
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        args.append(value)
        sets.append(f"{key}=${len(args)}")
    if not sets:
        return
    args.extend([run_id, case_id])
    await execute(
        f"UPDATE telemetry.eval_run_items SET {', '.join(sets)}, updated_at=now() "
        f"WHERE run_id=${len(args) - 1} AND case_id=${len(args)}",
        *args)


async def list_items(run_id: int, limit: int = 500) -> list[dict]:
    """run 内逐题结果 + 题面（前端结果表与服务端汇总共用）。"""
    rows = await fetch(
        """
        SELECT i.*, c.question, c.difficulty, c.ops, c.grain, c.db_name
        FROM telemetry.eval_run_items i
        JOIN telemetry.eval_cases c ON c.id = i.case_id
        WHERE i.run_id=$1 ORDER BY i.case_id LIMIT $2
        """, run_id, limit)
    return [dict(r) for r in rows]


# ============================================================ trace 派生指标

async def trace_stats(trace_id: str) -> dict:
    """从 trace 派生：决策步数（llm span 数）、工具调用/失败数、上下文占用、耗时。"""
    if not trace_id:
        return {}
    row = await fetchrow(
        """
        SELECT t.duration_ms,
               COALESCE(NULLIF(t.tags->>'tool_calls', '')::int, 0) AS tool_calls,
               COALESCE(NULLIF(t.tags->>'context_usage', '')::numeric, 0) AS context_usage,
               (SELECT count(*) FROM telemetry.trace_spans s
                 WHERE s.trace_id=t.trace_id AND s.op_type='llm') AS steps,
               (SELECT count(*) FROM telemetry.trace_spans s
                 WHERE s.trace_id=t.trace_id AND s.op_type='tool' AND s.status='error') AS tool_failures
        FROM telemetry.traces t WHERE t.trace_id=$1
        """, trace_id)
    if row is None:
        return {}
    out = dict(row)
    return {
        "steps": int(out["steps"] or 0),
        "tool_calls": int(out["tool_calls"] or 0),
        "tool_failures": int(out["tool_failures"] or 0),
        "context_usage": float(out["context_usage"] or 0),
        "duration_ms": float(out["duration_ms"] or 0),
    }


# ============================================================ 6 维汇总

def aggregate(items: list[dict]) -> dict:
    """逐题结果 → 6 维指标 + 失败归类。

    维度：准确性(EX) / 有效性(VES) / SQL 有效率(valid_sql) / 工具成功率 /
          平均步数 / 平均耗时（上下文占用单独给出，供报告解读）。
    """
    scored = [i for i in items if i.get("ex") is not None]
    tool_calls = sum(int(i.get("tool_calls") or 0) for i in scored)
    tool_failures = sum(int(i.get("tool_failures") or 0) for i in scored)
    context = [float(i["context_usage"]) for i in scored if i.get("context_usage") is not None]
    categories: dict[str, int] = {}
    for item in items:
        if item.get("status") == ITEM_FAILED:
            categories["run_error"] = categories.get("run_error", 0) + 1
        elif item.get("category"):
            key = item["category"]
            categories[key] = categories.get(key, 0) + 1
    return {
        "ex": _avg([float(i["ex"]) for i in scored]),
        "ves": _avg([float(i["ves"] or 0) for i in scored]),
        "valid_sql": _avg([float(i["valid_sql"] or 0) for i in scored]),
        # 工具成功率：失败次数 / 调用次数（无调用给 None，避免"没调工具"被算成 100%）
        "tool_success_rate": round(1 - tool_failures / tool_calls, 4) if tool_calls else None,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "avg_steps": _avg([float(i["steps"]) for i in scored if i.get("steps") is not None]),
        "avg_duration_ms": _avg([float(i["duration_ms"]) for i in scored if i.get("duration_ms") is not None]),
        "context_usage_avg": _avg(context),
        # 压缩率口径：上下文占用峰值超过 80% 的题占比（越高说明越依赖摘要压缩）
        "context_compress_ratio": (
            round(sum(1 for c in context if c >= CONTEXT_BREACH_RATIO) / len(context), 4)
            if context else None
        ),
        "case_scored": len(scored),
        "categories": categories,
    }


def render_report(run: dict, items: list[dict], metrics: dict) -> str:
    """生成 Markdown 汇总报告：指标表 + 失败归因 + 失败题明细（供前端直接展示）。"""
    def fmt(value, suffix: str = "") -> str:
        if value is None:
            return "-"
        return f"{value * 100:.1f}%{suffix}" if isinstance(value, float) and 0 <= value <= 1 else f"{value}{suffix}"

    lines = [
        f"# 评测报告 · {run.get('name') or run.get('id')}",
        "",
        f"- 状态：{run.get('status')}  题数：{run.get('case_total')}  "
        f"完成：{run.get('case_done')}  失败：{run.get('case_failed')}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 准确性 EX | {fmt(metrics.get('ex'))} |",
        f"| 有效性 VES | {fmt(metrics.get('ves'))} |",
        f"| SQL 有效率 | {fmt(metrics.get('valid_sql'))} |",
        f"| 工具成功率 | {fmt(metrics.get('tool_success_rate'))} |",
        f"| 平均步数 | {metrics.get('avg_steps') or '-'} |",
        f"| 平均耗时 | {round(metrics['avg_duration_ms'] / 1000, 1) if metrics.get('avg_duration_ms') else '-'} s |",
        f"| 上下文占用均值 | {fmt(metrics.get('context_usage_avg'))} |",
        f"| 上下文压缩率 | {fmt(metrics.get('context_compress_ratio'))} |",
        "",
    ]
    categories = metrics.get("categories") or {}
    if categories:
        lines += ["## 失败归因", "", "| 类别 | 题数 |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in sorted(categories.items(), key=lambda kv: -kv[1])]
        lines.append("")
    failed = [i for i in items if i.get("ex") == 0 or i.get("status") == ITEM_FAILED]
    if failed:
        lines += ["## 失败题目", ""]
        for item in failed[:20]:
            detail = (item.get("detail") or item.get("error") or "")[:160]
            lines.append(f"- case {item['case_id']}｜{item.get('category') or item.get('status')}｜"
                         f"{(item.get('question') or '')[:60]}｜{detail}")
        lines.append("")
    return "\n".join(lines)
