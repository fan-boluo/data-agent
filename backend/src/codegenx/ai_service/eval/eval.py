"""T1 评测闭环 CLI（读库模式）：金标准题集 → 重放产生 trace → 读库评分写回 → 聚合报告。

子命令：
  import-cases  题集 JSON 导入 eval_cases（按 dataset+question upsert）
  replay        逐题调用 POST /ai/chat/gen 产生 trace（trace_id 编码 case_id）
  score         读 telemetry trace/spans 取 SQL，连业务库对比 gold_sql 算 EX/VES/ValidRate → 写 trace_scores
  report        聚合 trace_scores 输出 Markdown 报告

题集 JSON 格式：
  {"dataset": "pg_test", "description": "...", "cases": [
     {"question": "...", "gold_sql": "SELECT ...", "db_name": "business",
      "difficulty": "simple", "ops": "agg", "grain": "city", "multi_hop": false}]}

运行（cwd 任意）：
  backend/.venv/Scripts/python.exe backend/src/codegenx/ai_service/eval/eval.py <subcommand> ...

 d:/Project/agent/CodeGenX/backend/.venv/Scripts/python.exe backend/src/codegenx/ai_service/eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --ids 25,26,27,35,36,37,47,48,50
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import httpx

# 本文件位于 backend/src/codegenx/ai_service/eval/，向上 3 层为 backend/src
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_REPO = _SRC.parents[1]  # 仓库根目录（题集/报告等静态资源仍放 scripts/eval 下）

from db.postgres.postgres_client import postgres_business, postgres_dsn  # noqa: E402
from codegenx.ai_service.eval.scorer import (  # noqa: E402 — 评分口径唯一真源（CLI 与服务端共用）
    LOOSE_ROW_LIMIT,
    SQL_TIMEOUT_S,
    TOP_N_TOLERANCE,
    TRACE_WAIT_S,
    _date_prefix_hit,
    _depaginate,
    _json_default,
    _load_gold_cache,
    _load_sqls,
    _loose_tokens,
    _norm_value,
    _row_covered,
    _row_values,
    _sig,
    fetch_rows,
    rows_match,
    rows_match_relaxed,
    score_case,
    write_scores,
)
from shared.config.config import get_settings  # noqa: E402
from shared.constants import PUBLIC_SCHEMAS  # noqa: E402

EVAL_PREFIX = "eval"          # 重放 trace_id 前缀：eval-{case_id}-{随机}，score 据此关联


# ============================================================ 元库访问

async def meta_conn() -> asyncpg.Connection:
    return await asyncpg.connect(dsn=postgres_dsn())


def _since(s: str) -> datetime:
    """--since 参数 → tz-aware UTC（asyncpg timestamptz 不接受 naive datetime）。

    未带时区后缀时按本机时区解释：与 PostgreSQL 手写
    `started_at > '2026-11-05T12:00:00'`（naive 字面量按会话时区解释）语义一致。
    原先强制按 UTC 解释，导致本地 12:00 的时间点被当成 UTC 12:00（本地 20:00），
    本地 12:00~20:00 之间产生的 trace 全部被漏掉。
    """
    dt = datetime.fromisoformat(s) if s else datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → 本机时区
    return dt.astimezone(timezone.utc)


# ============================================================ 子命令：import-cases

async def cmd_import(args) -> None:
    """题集 JSON → eval_datasets / eval_cases（dataset+question 唯一，upsert）。

    支持两种格式：
      1. {"dataset","description","cases":[...]}（标准格式）
      2. 扁平数组 [{question, sql, difficulty, ops, ...}]（question.json 格式，
         sql 映射为 gold_sql；dataset 名取 --name 或文件名，db_name 取 --db）
    """
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        data = {
            "dataset": args.name or Path(args.file).stem,
            "description": args.description,
            "cases": [{**c, "gold_sql": str(c.get("sql") or "")} for c in raw],
        }
    else:
        data = raw
        data["dataset"] = data.get("dataset") or args.name or Path(args.file).stem
    name = data["dataset"]
    conn = await meta_conn()
    try:
        ds_id = await conn.fetchval(
            """
            INSERT INTO telemetry.eval_datasets (name, description)
            VALUES ($1, $2)
            ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description
            RETURNING id
            """, name, data.get("description") or "")
        n_new = n_upd = 0
        for case in data.get("cases", []):
            if isinstance(case.get("ops"), list):
                case["ops"] = ",".join(str(o) for o in case["ops"])
            question = str(case.get("question") or "").strip()
            if not question:
                continue
            case_code = str(case.get("id") or "").strip() or None
            # 优先按 case_code 覆盖（JSON 里的 id 字段），没 case_code 时按 question 兜底
            if case_code:
                row = await conn.fetchrow(
                    """
                    INSERT INTO telemetry.eval_cases
                      (dataset_id, case_code, question, gold_sql, db_name, difficulty, ops, grain, multi_hop, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (dataset_id, case_code) DO UPDATE SET
                      question = EXCLUDED.question,
                      gold_sql = EXCLUDED.gold_sql, db_name = EXCLUDED.db_name,
                      difficulty = EXCLUDED.difficulty, ops = EXCLUDED.ops, grain = EXCLUDED.grain,
                      multi_hop = EXCLUDED.multi_hop, status = EXCLUDED.status, updated_at = now()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    ds_id, case_code, question, str(case.get("gold_sql") or ""),
                    str(args.db or case.get("db_name") or ""),
                    str(case.get("difficulty") or ""), str(case.get("ops") or ""),
                    str(case.get("grain") or ""), bool(case.get("multi_hop")),
                    str(args.status or case.get("status") or "draft"),
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO telemetry.eval_cases
                      (dataset_id, question, gold_sql, db_name, difficulty, ops, grain, multi_hop, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (dataset_id, question) DO UPDATE SET
                      gold_sql = EXCLUDED.gold_sql, db_name = EXCLUDED.db_name,
                      difficulty = EXCLUDED.difficulty, ops = EXCLUDED.ops, grain = EXCLUDED.grain,
                      multi_hop = EXCLUDED.multi_hop, status = EXCLUDED.status, updated_at = now()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    ds_id, question, str(case.get("gold_sql") or ""), str(args.db or case.get("db_name") or ""),
                    str(case.get("difficulty") or ""), str(case.get("ops") or ""),
                    str(case.get("grain") or ""), bool(case.get("multi_hop")),
                    str(args.status or case.get("status") or "draft"),
                )
            n_new += 1 if row["inserted"] else 0
            n_upd += 0 if row["inserted"] else 1
        total = await conn.fetchval(
            "SELECT count(*) FROM telemetry.eval_cases WHERE dataset_id=$1", ds_id)
        print(f"[OK] 题集 {name}(id={ds_id}) 导入：新增 {n_new}，更新 {n_upd}，共 {total} 题")
    finally:
        await conn.close()


# ============================================================ 子命令：replay

async def _login(base_url: str, account: str, password: str) -> str:
    async with httpx.AsyncClient(base_url=base_url, timeout=15, trust_env=False) as client:
        resp = await client.post("/api/user/login", json={
            "userAccount": account, "userPassword": password})
        resp.raise_for_status()
        body = resp.json()
        token = (body.get("data") or "")
        if not token:
            raise RuntimeError(f"登录失败: {body}")
        return token


async def _wait_trace(conn: asyncpg.Connection, trace_id: str) -> bool:
    """轮询等 turn_end flush 落库。"""
    deadline = time.time() + TRACE_WAIT_S
    while time.time() < deadline:
        if await conn.fetchval("SELECT 1 FROM telemetry.traces WHERE trace_id=$1", trace_id):
            return True
        await asyncio.sleep(0.5)
    return False


async def cmd_replay(args) -> None:
    """逐题重放：每题独立会话，trace_id = eval-{case_id}-{rand}；SSE 读到流结束。

    默认重放所有 verified 题（按 dataset 过滤用 --dataset，指定具体 case 用 --ids）。
    """
    token = await _login(args.base_url, args.account, args.password)
    conn = await meta_conn()
    try:
        cond = "status='verified'" if not args.all else "TRUE"
        params: list = []
        if args.ids:
            cond += " AND c.id = ANY($1)"
            params.append([int(x) for x in args.ids.split(",") if x.strip()])
        if args.dataset:
            # 按 dataset name 过滤
            ds_ids = [r['id'] for r in await conn.fetch(
                "SELECT id FROM telemetry.eval_datasets WHERE name = ANY($1)",
                args.dataset.split(","))]
            if not ds_ids:
                print(f"[WARN] dataset '{args.dataset}' 未找到任何 eval_datasets，无题目重放")
                return
            cond += f" AND c.dataset_id = ANY(${len(params)+1})"
            params.append(ds_ids)
        params.append(args.limit)
        cases = await conn.fetch(
            f"SELECT c.* FROM telemetry.eval_cases c WHERE {cond} ORDER BY c.id LIMIT ${len(params)}", *params)
        print(f"[OK] 待重放 {len(cases)} 题 → {args.base_url}")
        ok = fail = 0
        async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout,
                                     trust_env=False) as client:
            headers = {"Authorization": f"Bearer {token}"}
            for case in cases:
                trace_id = f"{EVAL_PREFIX}-{case['id']}-{uuid.uuid4().hex[:8]}"
                payload = {
                    "appId": args.app_id, "message": case["question"],
                    "sessionId": f"evalsess-{trace_id}",
                    "traceId": trace_id, "requestId": f"evalreq-{trace_id}",
                    "clientVersion": "eval-script",
                }
                if case["db_name"]:
                    payload["dbName"] = case["db_name"]
                    # 多数据集改造后 agent 只聚焦 currentDb（datasource.schema）；
                    # 由 db_name 从 PUBLIC_SCHEMAS 反查对应条目
                    current_db = next(
                        (s for s in PUBLIC_SCHEMAS if s.split(".", 1)[0] == case["db_name"]), None)
                    if current_db:
                        payload["currentDb"] = current_db
                t0 = time.time()
                try:
                    # SSE：读到流结束即 turn 结束（内容不需解析）
                    async with client.stream("POST", "/api/ai/chat/gen", json=payload, headers=headers) as resp:
                        if resp.status_code != 200:
                            raise RuntimeError(f"HTTP {resp.status_code}: {(await resp.aread())[:200]}")
                        async for _ in resp.aiter_text():
                            pass
                except Exception as exc:
                    print(f"[FAIL] case={case['id']} {exc}")
                    fail += 1
                    continue
                landed = await _wait_trace(conn, trace_id)
                mark = "OK" if landed else "NO-TRACE"
                ok += 1 if landed else 0
                fail += 0 if landed else 1
                print(f"[{mark}] case={case['id']} trace={trace_id} {time.time()-t0:.1f}s")
                await asyncio.sleep(args.interval)
        print(f"[DONE] 重放完成：落库 {ok}，未落库/失败 {fail}")
    finally:
        await conn.close()


# ============================================================ 子命令：score


async def cmd_score(args) -> None:
    """读库评分：pred 取 trace 最后一次 SQL 尝试，gold 执行对比；写回 trace_scores。"""
    conn = await meta_conn()
    try:
        if args.trace_id:
            traces = await conn.fetch("SELECT trace_id FROM telemetry.traces WHERE trace_id=$1", args.trace_id)
        else:
            # 默认评全部重放 trace；--since 只评某时刻之后（避免重复）
            since = _since(args.since)
            traces = await conn.fetch(
                "SELECT trace_id FROM telemetry.traces WHERE trace_id LIKE $1 AND started_at > $2",
                f"{EVAL_PREFIX}-%", since)
        print(f"[OK] 待评分 trace {len(traces)} 条")
        cache: dict[str, asyncpg.Connection] = {}
        n_scored = n_skip = 0
        try:
            for row in traces:
                trace_id = row["trace_id"]
                m = re.match(rf"{EVAL_PREFIX}-(\d+)-", trace_id)
                if not m:
                    n_skip += 1
                    continue
                case = await conn.fetchrow("SELECT * FROM telemetry.eval_cases WHERE id=$1", int(m.group(1)))
                if not case or not case["gold_sql"]:
                    n_skip += 1
                    continue
                result = await score_case(conn, cache, trace_id, case)
                await write_scores(conn, trace_id, case["id"], result)
                n_scored += 1
                print(f"[{result['category'].upper()}] trace={trace_id} EX={result['ex']} VES={result['ves']} {result['category']}")
        finally:
            for c in cache.values():
                await c.close()
        print(f"[DONE] 评分 {n_scored} 条，跳过 {n_skip} 条")
    finally:
        await conn.close()




# ============================================================ 子命令：report

def _norm_sql(sql: str) -> str:
    """SQL 文本规范化（压空白、小写、去结尾分号），报告判断 agent SQL 与 gold 文本是否一致。"""
    return " ".join((sql or "").split()).lower().rstrip(";")


def _extract_tables(sql: str) -> list[str]:
    """粗提 SQL 的 FROM/JOIN 表名（去引号/别名；子查询内的 FROM 由逐位置匹配覆盖，
    仅用于报告展示级对比）。"""
    tables: list[str] = []
    for m in re.finditer(r"(?is)\b(?:from|join)\s+([\w\".]+(?:\s*,\s*[\w\".]+)*)", sql):
        for tok in m.group(1).split(","):
            tok = tok.strip().strip('"').lower()
            tok = tok.split()[0] if tok else ""
            if tok and tok != "select" and tok not in tables:
                tables.append(tok)
    return tables


def _table_leaf(t: str) -> str:
    """表名取末段（兼容 schema.table / db.schema.table 与召回裸名对比）。"""
    return t.strip('"').split(".")[-1].lower()


def _fence(text: str, lang: str = "") -> list[str]:
    """用代码围栏包裹任意文本；内部反引号串更长时自动加长围栏，避免提前闭合。"""
    body = str(text or "").rstrip("\n")
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", body, fence]


def _block(body: str, lang: str = "text") -> list[str]:
    """长文本块：空行 + 顶格代码围栏。

    不用 <details> 包裹：raw HTML 块内是否解析 Markdown 各渲染器行为不一致
    （VS Code 会显示字面围栏，部分渲染器会把 LLM 输出里的 # 当报告标题）。
    顶格围栏 + 空行分隔是纯 Markdown，任何渲染器表现一致。
    """
    return ["", *_fence(body, lang), ""]


def _cell(text: str, limit: int = 90) -> str:
    """表格单元格文本：压成单行、转义竖线、超长截断。"""
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s[:limit] + ("…" if len(s) > limit else "")


def _pct(values: list[float], p: float) -> float:
    """分位数（最近秩），空列表返回 0。"""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))]


def _sec(ms: float) -> str:
    """毫秒 → 秒（1 位小数）；未知耗时显示 -。"""
    return f"{ms / 1000:.1f}s" if ms else "-"


def _num(v: float) -> str:
    """整数千分位；0 视为未采集。"""
    return f"{v:,.0f}" if v else "-"


def _json_obj(v) -> dict:
    """JSONB 字段 → dict；兼容空值、"null" 字符串与脏数据。"""
    if isinstance(v, dict):
        return v
    try:
        obj = json.loads(v or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


async def _render_case(conn: asyncpg.Connection, t: asyncpg.Record, ves, human: dict) -> str:
    """单题渲染：头部指标 → 执行链路（think/tool 序列）→ 召回评估 → 分析/回复人工位。

    链路自 trace_spans（时序骨架）+ tool_semantics（sql/recalled_tables 语义）拼出；
    SQL 只写成败与行数，不写返回结果。
    """
    tid = t["trace_id"]
    spans = await conn.fetch(
        """
        SELECT span_id, op_type, name, agent, status, duration_ms, output
        FROM telemetry.trace_spans WHERE trace_id=$1 ORDER BY seq
        """, tid)
    sems = await conn.fetch(
        """
        SELECT tool_name, span_id, success, tool_args, tool_result
        FROM telemetry.tool_semantics WHERE trace_id=$1 ORDER BY created_at
        """, tid)
    sem_by_span: dict = {}
    sqls: list[dict] = []
    recalled: list[str] = []
    for r in sems:
        args = _json_obj(r["tool_args"])
        res = _json_obj(r["tool_result"])
        if r["tool_name"] == "execute_postgresql":
            sql = str(args.get("sql") or "").strip()
            if sql:
                sqls.append({"sql": sql, "ok": bool(r["success"]), "rows": res.get("row_count")})
            if r["span_id"]:
                sem_by_span[r["span_id"]] = {"sql": sql, "rows": res.get("row_count")}
        tables = res.get("recalled_tables")
        if tables:
            if isinstance(tables, str):
                try:
                    tables = json.loads(tables)
                except Exception:
                    tables = [tables]
            for tb in tables:
                leaf = _table_leaf(str(tb))
                if leaf and leaf not in recalled:
                    recalled.append(leaf)

    lines = [
        f"### Case {t['case_id']}：{t['question']}",
        "",
        f"- 标签：{t['difficulty'] or '-'} / {t['ops'] or '-'} / {'多跳' if t['multi_hop'] else '单跳'}",
    ]
    # 效率指标：耗时 / tokens（token 未采集时只出耗时）
    pt, ct = int(t["prompt_tokens"] or 0), int(t["completion_tokens"] or 0)
    eff = f"- 耗时：{_sec(float(t['duration_ms'] or 0))}"
    if pt or ct:
        eff += f" ｜ tokens：{_num(pt)} / {_num(ct)}"
    lines += [
        eff,
        f"- EX：{t['ex']:.0%}（{t['category'] or '-'}）｜ VES：{f'{ves:.3f}' if ves is not None else '-'}"
        f" ｜ 状态：{t['status']} ｜ trace：`{tid}`",
        "",
        "**执行链路**",
        "",
    ]
    # 最终回答取链路末尾无 tool_calls 的 llm span（traces 不存输入输出快照）
    final_sid = ""
    for sp in reversed(spans):
        if sp["op_type"] != "llm":
            continue
        out = _json_obj(sp["output"])
        if out.get("tool_calls"):
            continue
        if str(out.get("content") or "").strip():
            final_sid = sp["span_id"]
            break

    cur_agent, step = "", 0
    for sp in spans:
        if sp["op_type"] == "turn":
            continue
        if sp["agent"] and sp["agent"] != cur_agent:
            cur_agent = sp["agent"]
            lines += [f"> agent → {cur_agent}", ""]
        step += 1
        out = _json_obj(sp["output"])
        ms = f"{sp['duration_ms']:.0f}ms" if sp["duration_ms"] is not None else ""
        ok = "✓" if sp["status"] == "ok" else "✗"
        if sp["op_type"] == "llm":
            tcs = out.get("tool_calls") or []
            content = str(out.get("content") or "").strip()
            if tcs:  # 有 tool_calls 的 llm 输出即 Thought 步骤
                names = "、".join(str(c.get("name") or (c.get("function") or {}).get("name") or "?") for c in tcs)
                lines.append(f"{step}. **Thought** {ok}（{ms}）→ 调用 {names}")
                if content:
                    lines += _block(content)
            elif content:
                title = "回答（最终）" if sp["span_id"] == final_sid else "中间回答"
                lines.append(f"{step}. **{title}** {ok}（{ms}）")
                lines += _block(content)
        elif sp["op_type"] == "tool" and sp["name"] == "execute_postgresql":
            sem = sem_by_span.get(sp["span_id"] or "") or {}
            rows = sem.get("rows")
            lines.append(f"{step}. **SQL 执行** {ok}（{rows if rows is not None else '?'} 行{'，' + ms if ms else ''}）")
            sql = sem.get("sql") or ""
            if sql:
                lines += _block(sql, "sql")
        else:
            meta = f"（{ms}）" if ms else ""
            lines.append(f"{step}. **{sp['op_type']}** {sp['name']} {ok}{meta}")
            lines.append("")
    if not final_sid:
        lines.append("**最终回答**：（无）")
    lines.append("")

    # 召回评估：gold SQL 全文 + agent 各条 SQL（文本比对）+ 表召回对比
    gold_sql = t["gold_sql"]
    gnorm = _norm_sql(gold_sql)
    lines += ["**召回评估**", "", "- gold_sql：", ""]
    lines += _block(gold_sql, "sql")
    if not sqls:
        lines.append("- Agent SQL：无（no_sql）")
    else:
        lines.append(f"- Agent SQL（{len(sqls)} 条，✓=文本与 gold 一致）：")
        lines.append("")
        for i, s in enumerate(sqls, 1):
            hit = "✓" if _norm_sql(s["sql"]) == gnorm else "✗"
            meta = f"{'成功' if s['ok'] else '失败'}，{s['rows'] if s['rows'] is not None else '?'} 行"
            lines.append(f"  {i}. {hit}（{meta}）")
            lines += _block(s["sql"], "sql")
    gold_tables = sorted({_table_leaf(x) for x in _extract_tables(gold_sql)})
    missing = [x for x in gold_tables if x not in recalled]
    lines.append(f"- 结果命中（EX）：{t['ex']:.0%}")
    lines.append(f"- 表召回：gold {gold_tables or '∅'} ｜ 检索到 {recalled or '∅'} ｜ "
                 + ("全部命中" if not missing else f"缺失 {missing}"))
    if any("information_schema" in s["sql"].lower() for s in sqls):
        lines.append("- 注：存在 information_schema 自探（绕过语义召回获取表结构）")
    lines.append("")

    # 分析/回复：预留人工评估（写 trace_scores evaluator='human', name='analysis'/'answer'）
    lines += [
        f"**分析评估**（人工）：{human.get('analysis') or '待人工评估'}",
        "",
        f"**回复评估**（人工）：{human.get('answer') or '待人工评估'}",
        "",
    ]
    return "\n".join(lines)


# 报告重点标签：只统计有区分度的数仓特征标签（通用语法类标签不计入统计）
_KEY_OPS = {
    "cte", "window_function", "correlated_subquery", "subquery", "union",
    "grouping_set", "case_when", "having", "ratio_calc", "filter_null",
}


async def cmd_report(args) -> None:
    """评测报告：汇总指标 + 题目概览 + 报告概览（难度/重点标签/逐题清单）+ 逐题渲染。"""
    conn = await meta_conn()
    try:
        since = _since(args.since)
        ex = await conn.fetchval(
            """
            SELECT avg(s.value) FROM telemetry.trace_scores s
            WHERE s.name='EX' AND s.evaluator='eval_script' AND s.created_at > $1
            """, since)
        valid = await conn.fetchval(
            """
            SELECT avg(s.value) FROM telemetry.trace_scores s
            WHERE s.name='valid_sql' AND s.evaluator='eval_script' AND s.created_at > $1
            """, since)
        ves = await conn.fetchval(
            """
            SELECT avg(s.value) FROM telemetry.trace_scores s
            WHERE s.name='VES' AND s.evaluator='eval_script' AND s.created_at > $1
            """, since)
        n = await conn.fetchval(
            """
            SELECT count(DISTINCT trace_id) FROM telemetry.trace_scores
            WHERE name='EX' AND evaluator='eval_script' AND created_at > $1
            """, since)
        # ── 报告概览：按 case 汇总（同一 case 多次重放只取最新一次 EX）──
        case_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (s.case_id)
                   s.case_id, s.trace_id, s.value AS ex, s.comment AS category,
                   c.difficulty, c.ops, c.multi_hop
            FROM telemetry.trace_scores s
            JOIN telemetry.eval_cases c ON c.id = s.case_id
            WHERE s.name='EX' AND s.evaluator='eval_script' AND s.created_at > $1
            ORDER BY s.case_id, s.created_at DESC
            """, since)
        detail_map = {
            r["trace_id"]: r["comment"]
            for r in await conn.fetch(
                """
                SELECT trace_id, comment FROM telemetry.trace_scores
                WHERE name='detail' AND evaluator='eval_script' AND created_at > $1
                """, since)
        }

        # ── 题目概览：评测覆盖的 eval_cases 统计 ──
        cases_scored = await conn.fetch(
            """
            SELECT DISTINCT c.*, d.name AS dataset_name
            FROM telemetry.eval_cases c
            JOIN telemetry.eval_datasets d ON d.id = c.dataset_id
            JOIN telemetry.trace_scores s ON s.case_id = c.id
            WHERE s.name='EX' AND s.evaluator='eval_script' AND s.created_at > $1
            """, since)

        # dataset / difficulty / grain / ops 分布
        ds_counter: dict = {}
        diff_counter: dict = {}
        grain_counter: dict = {}
        ops_counter: dict = {}

        for c in cases_scored:
            ds_name = c.get('dataset_name') or c.get('db_name') or '?'
            ds_counter[ds_name] = ds_counter.get(ds_name, 0) + 1
            diff_counter[c.get('difficulty') or '?'] = diff_counter.get(c.get('difficulty') or '?', 0) + 1
            grain_counter[c.get('grain') or '?'] = grain_counter.get(c.get('grain') or '?', 0) + 1
            # ops 是逗号分隔 text
            for op in (c.get('ops') or '').split(','):
                op = op.strip()
                if op:
                    ops_counter[op] = ops_counter.get(op, 0) + 1

        # ops 统计 Top 10（按使用频次）
        ops_sorted = sorted(ops_counter.items(), key=lambda x: -x[1])[:10]

        # 报告概览：难度 / 重点标签的题数与 EX（每 case 一条）
        diff_stat: dict[str, list] = {}
        op_stat: dict[str, list] = {}
        for r in case_rows:
            score = r["ex"] or 0.0
            diff_stat.setdefault(r["difficulty"] or "?", []).append(score)
            for op in (r["ops"] or "").split(","):
                op = op.strip()
                if op in _KEY_OPS:
                    op_stat.setdefault(op, []).append(score)

        # ── 成本与效率：按 case 最新 trace 聚合（口径与 EX 一致，一题一条）──
        trace_ids = [r["trace_id"] for r in case_rows if r["trace_id"]]
        perf_raw: dict[str, dict] = {}
        if trace_ids:
            for r in await conn.fetch(
                """
                SELECT t.trace_id, t.duration_ms, t.model,
                       t.prompt_tokens, t.completion_tokens,
                       COALESCE((t.tags->>'tool_calls')::int, 0) AS tool_calls
                FROM telemetry.traces t WHERE t.trace_id = ANY($1)
                """, trace_ids):
                perf_raw[r["trace_id"]] = dict(r)
            # llm 调用次数只在 spans 里，traces 无对应列
            for r in await conn.fetch(
                """
                SELECT trace_id, count(*) AS llm_calls FROM telemetry.trace_spans
                WHERE trace_id = ANY($1) AND op_type='llm' GROUP BY trace_id
                """, trace_ids):
                perf_raw.setdefault(r["trace_id"], {})["llm_calls"] = r["llm_calls"]

        def _perf_of(tid: str) -> dict:
            p = perf_raw.get(tid) or {}
            pt = int(p.get("prompt_tokens") or 0)
            ct = int(p.get("completion_tokens") or 0)
            return {
                "dur": float(p["duration_ms"]) if p.get("duration_ms") else 0.0,
                "pt": pt, "ct": ct, "tokens": pt + ct,
                "tool": int(p.get("tool_calls") or 0),
                "llm": int(p.get("llm_calls") or 0),
            }

        perf_all = [_perf_of(r["trace_id"]) for r in case_rows]
        perf_pass = [_perf_of(r["trace_id"]) for r in case_rows if (r["ex"] or 0) >= 0.5]
        perf_fail = [_perf_of(r["trace_id"]) for r in case_rows if (r["ex"] or 0) < 0.5]
        # token 覆盖不足时（历史 trace 无 usage）不出均值/金额，避免报告出现失真成本
        token_cov = sum(1 for p in perf_all if p["tokens"] > 0)
        has_token = bool(perf_all) and token_cov == len(perf_all)

        def _agg(rows: list[dict]) -> dict:
            n = len(rows)
            if not n:
                return {}
            durs = [p["dur"] for p in rows if p["dur"] > 0]
            return {
                "n": n,
                "dur_avg": sum(durs) / len(durs) if durs else 0.0,
                "tool_avg": sum(p["tool"] for p in rows) / n,
                "llm_avg": sum(p["llm"] for p in rows) / n,
                "pt_avg": sum(p["pt"] for p in rows) / n,
                "ct_avg": sum(p["ct"] for p in rows) / n,
                "token_avg": sum(p["tokens"] for p in rows) / n,
                "pt_sum": sum(p["pt"] for p in rows),
                "ct_sum": sum(p["ct"] for p in rows),
                "durs": durs,
            }

        # ── 组装报告 ──
        lines = [
            "# 评测报告", "",
            f"- 评测范围：{since.astimezone():%Y-%m-%d %H:%M:%S}（本机时间）之后",
            f"- **评测题数**：{len(cases_scored)} 题（共 {n} 条 trace 记录）",
            f"- **EX**：{ex:.2%}" if ex is not None else "- EX：无数据",
            f"- **Valid SQL Rate**：{valid:.2%}" if valid is not None else "- Valid SQL Rate：无数据",
            f"- **VES**：{ves:.3f}" if ves is not None else "- VES：无数据",
            "",
        ]

        # ── 成本与效率：单题均值 + 通过/失败对比（失败题更贵是迭代优先级的依据）──
        ov = _agg(perf_all)
        if ov:
            lines += ["## 成本与效率", "", "| 指标 | 值 |", "|---|---|",
                      f"| 平均单题耗时 | {_sec(ov['dur_avg'])} |",
                      f"| 耗时 P50 / P95 | {_sec(_pct(ov['durs'], 0.5))} / {_sec(_pct(ov['durs'], 0.95))} |",
                      f"| 平均 LLM 调用 | {ov['llm_avg']:.1f} 次 |",
                      f"| 平均工具调用 | {ov['tool_avg']:.1f} 次 |"]
            if has_token:
                lines += [f"| 平均单题 tokens（prompt / completion） | {_num(ov['pt_avg'])} / {_num(ov['ct_avg'])} |",
                          f"| 评测总 tokens（prompt / completion） | {_num(ov['pt_sum'])} / {_num(ov['ct_sum'])} |"]
            else:
                lines.append(f"| tokens | 未完整采集（{token_cov}/{len(perf_all)} 题有 usage，"
                             f"重放后补齐） |")
            lines += ["", "### 通过 vs 失败（效率对比）", "",
                      "| 分组 | 题数 | 平均耗时 | 平均 LLM 调用 | 平均工具调用 | 平均 tokens |",
                      "|---|---|---|---|---|---|"]
            for label, group in (("通过", perf_pass), ("失败", perf_fail)):
                a = _agg(group)
                if not a:
                    continue
                tokens = _num(a['token_avg']) if has_token else "-"
                lines.append(
                    f"| {label} | {a['n']} | {_sec(a['dur_avg'])} | {a['llm_avg']:.1f} "
                    f"| {a['tool_avg']:.1f} | {tokens} |")
            lines.append("")

        lines += ["## 题目概览", "",
            "### 数据集", "",
            "| dataset | 题数 |", "|---|---|",
        ]
        for ds, cnt in sorted(ds_counter.items(), key=lambda x: -x[1]):
            lines.append(f"| {ds} | {cnt} |")

        lines += ["", "### 难度分布", "", "| difficulty | 题数 |", "|---|---|"]
        for diff in ('easy', 'medium', 'hard'):
            cnt = diff_counter.get(diff, 0)
            if cnt > 0:
                lines.append(f"| {diff} | {cnt} |")
        # 其他 difficulty 值
        for diff, cnt in sorted(diff_counter.items(), key=lambda x: -x[1]):
            if diff not in ('easy', 'medium', 'hard'):
                lines.append(f"| {diff} | {cnt} |")

        lines += ["", "### 结果粒度 (grain)", "", "| grain | 题数 |", "|---|---|"]
        for g, cnt in sorted(grain_counter.items(), key=lambda x: -x[1]):
            lines.append(f"| {g} | {cnt} |")

        lines += ["", "### 操作标签 (ops Top 10)", "", "| op | 覆盖题数 |", "|---|---|"]
        for op, cnt in ops_sorted:
            lines.append(f"| `{op}` | {cnt} |")

        # ── 报告概览：难度分布 / 重点标签 / 逐题清单 ──
        def _stat_line(name: str, vals: list, with_label: bool) -> str:
            passed = sum(1 for v in vals if v >= 0.5)
            exv = sum(vals) / len(vals) if vals else 0.0
            tag = f"`{name}`" if with_label else name
            return f"| {tag} | {len(vals)} | {passed} | {exv:.2%} |"

        lines += ["", "## 报告概览", "", "### 难度分布（EX）", "",
                  "| 难度 | 题数 | 通过 | EX |", "|---|---|---|---|"]
        for diff in ("easy", "medium", "hard"):
            if diff in diff_stat:
                lines.append(_stat_line(diff, diff_stat[diff], False))
        for diff, vals in sorted(diff_stat.items()):
            if diff not in ("easy", "medium", "hard"):
                lines.append(_stat_line(diff, vals, False))

        lines += ["", "### 重点标签（EX）", "",
                  "| 标签 | 覆盖题数 | 通过 | EX |", "|---|---|---|---|"]
        # 按 EX 升序（弱项优先），EX 相同按题数降序
        for op, vals in sorted(op_stat.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), -len(kv[1]))):
            lines.append(_stat_line(op, vals, True))
        if not op_stat:
            lines.append("| 无 | 0 | 0 | - |")

        lines += ["", "### 题目结果清单", "",
                  "| case | 难度 | 标签 | 结果 | 失败归因 |", "|---|---|---|---|---|"]
        for r in case_rows:
            score = r["ex"] or 0.0
            if score >= 0.5:
                mark = "✅（宽松）" if r["category"] == "ok_relaxed" else "✅"
                reason = "-"
            else:
                mark = "❌"
                reason = _cell(f"{r['category'] or '-'}：{detail_map.get(r['trace_id'], '')}")
            lines.append(
                f"| {r['case_id']} | {r['difficulty'] or '-'} | {_cell(r['ops'] or '-')} "
                f"| {mark} | {reason} |")
        summary = "\n".join(lines)

        # 逐题段：所有已评分 trace（EX 分数存在即入报告），按 case id 排序
        rows = await conn.fetch(
            """
            SELECT t.trace_id, t.status, t.duration_ms,
                   t.prompt_tokens, t.completion_tokens,
                   s.value AS ex, s.comment AS category, s.case_id,
                   c.question, c.gold_sql, c.difficulty, c.ops, c.multi_hop
            FROM telemetry.traces t
            JOIN telemetry.trace_scores s
              ON s.trace_id = t.trace_id AND s.name='EX' AND s.evaluator='eval_script'
            JOIN telemetry.eval_cases c ON c.id = s.case_id
            WHERE t.trace_id LIKE $1 AND t.started_at > $2
            ORDER BY s.case_id, t.started_at
            """, f"{EVAL_PREFIX}-%", since)
        ves_map: dict = {}
        human: dict = {}
        if rows:
            for r in await conn.fetch(
                    "SELECT trace_id, name, value, comment, evaluator FROM telemetry.trace_scores WHERE trace_id = ANY($1)",
                    [r["trace_id"] for r in rows]):
                if r["name"] == "VES" and r["evaluator"] == "eval_script":
                    ves_map[r["trace_id"]] = r["value"]
                elif r["evaluator"] == "human":
                    mark = "" if r["value"] is None else f"{r['value']:g} "
                    human.setdefault(r["trace_id"], {})[r["name"]] = f"{mark}{r['comment'] or ''}".strip()
        parts = [await _render_case(conn, r, ves_map.get(r["trace_id"]), human.get(r["trace_id"], {}))
                 for r in rows]
        per_case = "\n\n---\n\n".join(parts) if parts else "（无评测记录）"
        report = f"{summary}\n\n---\n\n# 逐题评估\n\n{per_case}\n"

        # 文件名：用户显式 --out 则尊重，否则自动加时间戳，默认放 scripts/eval/report/ 子目录
        if args.out:
            out = Path(args.out)
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_dir = _REPO / "scripts" / "eval" / "report"
            report_dir.mkdir(parents=True, exist_ok=True)
            out = report_dir / f"report_{ts}.md"

        out.write_text(report, encoding="utf-8")
        print(summary)
        print(f"[OK] 报告已写 {out}（逐题 {len(parts)} 题）")
    finally:
        await conn.close()


# ============================================================ CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="T1 评测闭环（读库模式）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-cases", help="题集 JSON 导入 eval_cases")
    p.add_argument("--file", required=True, help="题集 JSON 路径")
    p.add_argument("--name", default="", help="dataset 名（默认取 JSON dataset 字段或文件名）")
    p.add_argument("--description", default="", help="dataset 描述（扁平格式时使用）")
    p.add_argument("--db", default="", help="覆盖业务库 db_name（扁平格式时使用，如 business）")
    p.add_argument("--status", default="", help="覆盖题目 status（verified/draft，默认 draft）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("replay", help="逐题调用 chat API 产生 trace")
    p.add_argument("--base-url", default=f"http://127.0.0.1:{get_settings().app_port}")
    p.add_argument("--account", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--app-id", default="main")
    p.add_argument("--ids", default="", help="只重放指定 case id，如 35,37（不填则重放全部 verified）")
    p.add_argument("--dataset", default="", help="按 dataset 名过滤，如 dvdrental 或 dvdrental,tpcds_sf1")
    p.add_argument("--limit", type=int, default=500, help="最多重放多少题（默认 500，覆盖完整题集）")
    p.add_argument("--all", action="store_true", help="含 draft 题目（默认只重放 verified）")
    p.add_argument("--timeout", type=int, default=300, help="单题 SSE 读取超时秒")
    p.add_argument("--interval", type=float, default=1.0, help="题间间隔秒")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("score", help="读库评分写回 trace_scores")
    p.add_argument("--trace-id", default="", help="只评指定 trace")
    p.add_argument("--since", default="", help="只评该 ISO 时刻之后（避免重复评分）")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("report", help="聚合输出 Markdown 报告")
    p.add_argument("--since", default="", help="聚合该 ISO 时刻之后")
    p.add_argument("--out", default="", help="输出路径（默认 scripts/eval/report/report_YYYYMMDD_HHMMSS.md）")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
