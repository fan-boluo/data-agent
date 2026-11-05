"""T1 评测闭环脚本（读库模式）：金标准题集 → 重放产生 trace → 读库评分写回 → 聚合报告。

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
  backend/.venv/Scripts/python.exe eval/eval.py <subcommand> ...

 d:/Project/agent/CodeGenX/backend/.venv/Scripts/python.exe eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --ids 25,26,27,35,36,37,47,48,50
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

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "backend" / "src"))

from db.postgres.postgres_client import postgres_business, postgres_dsn  # noqa: E402
from shared.config.config import get_settings  # noqa: E402
from shared.constants import PUBLIC_SCHEMAS  # noqa: E402

EVAL_PREFIX = "eval"          # 重放 trace_id 前缀：eval-{case_id}-{随机}，score 据此关联
SQL_TIMEOUT_S = 30            # 业务库单条 SQL 执行超时
TRACE_WAIT_S = 15             # 重放后等 trace 落库超时


# ============================================================ 结果对比（EX 核心）

_NULL_VALUES = {"", "null", "none", "nan", "nat", "-"}


def _norm_value(v) -> str:
    """值规范化：数值统一浮点格式（消除 100 vs 100.0），interval 统一为天数，
    日期统一为文本，NULL 类值统一为空串，其余字符串去空白小写。

    注意 gold_rows 走 JSONB 缓存，datetime 取回时是 ISO 字符串（T 分隔、可能带时区），
    与 agent 侧直接执行的 datetime 对象需归一，否则日期类题目会被误判为不匹配。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, timedelta):
        return f"{v.total_seconds() / 86400:.6f}".rstrip("0").rstrip(".")
    if isinstance(v, (int, float)):
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    if isinstance(v, datetime):
        # 零点时间统一为纯日期（兼容 date 与 timestamp 两种来源）
        if not (v.hour or v.minute or v.second):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    s = re.sub(r"\s+", " ", str(v)).strip()
    # 统一日期时间：T/空格分隔、丢弃小数秒（agent 侧 datetime 格式化到秒，
    # gold 缓存 JSON 保留微秒，截断后两侧可比）
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?", s)
    if m:
        s = m.group(1) if m.group(2) == "00:00:00" else f"{m.group(1)} {m.group(2)}"
    s = s.lower()
    return "" if s in _NULL_VALUES else s


def _depaginate(sql: str) -> str:
    """去掉结尾 LIMIT/OFFSET（top-N 并列时比较全序更公平），无变化返回空串。"""
    stripped = re.sub(
        r"(?is)\s+(?:LIMIT\s+\d+\s*(?:OFFSET\s+\d+)?|OFFSET\s+\d+)\s*;?\s*$", "", sql,
    ).strip()
    return stripped if stripped != sql.strip() else ""


def _sig(rows, cols: tuple) -> list:
    """行集在指定列投影下的规范化签名（多重集，忽略行序）。"""
    return sorted(tuple(_norm_value(r[c]) for c in cols) for r in rows)


def rows_match(pred_rows, gold_rows) -> bool:
    """EX 判定（列名优先、列位兜底，双向投影，多重集比较忽略行序）：

    1. gold 列名 ⊆ pred 列名：agent 多选列（附带上名列/中文别名场景按列位兜底）；
    2. pred 列名 ⊆ gold 列名：agent 少选列（如 gold SELECT * 含 last_update 这类
       不影响答案的列）；
    3. 列名无交集时退回列位组合投影（兼容全别名场景，组合数封顶防爆）。
    """
    if not gold_rows or not pred_rows:
        return not gold_rows and not pred_rows
    gkeys = [str(k) for k in gold_rows[0].keys()]
    pkeys = [str(k) for k in pred_rows[0].keys()]
    gset, pset = set(gkeys), set(pkeys)
    # 1. 列名：gold ⊆ pred
    if gset <= pset:
        return _sig(gold_rows, tuple(gkeys)) == _sig(pred_rows, tuple(gkeys))
    # 2. 列名：pred ⊆ gold
    if pset <= gset:
        return _sig(gold_rows, tuple(pkeys)) == _sig(pred_rows, tuple(pkeys))
    # 3. 列位兜底：gold 固定 sorted 键名序（JSONB 缓存不保 dict 序，键序不可靠），
    #    pred 侧枚举列排列做位置对齐（覆盖全/部分别名场景如 movie_count vs film_count；
    #    宽表排列爆炸时退回 sorted 对齐尽力而为）
    from itertools import permutations, islice
    gcols, pcols = len(gkeys), len(pkeys)
    if gcols > pcols:
        return False
    gold_sig = _sig(gold_rows, tuple(sorted(gkeys)))
    if pcols > 6:
        return gold_sig == _sig(pred_rows, tuple(sorted(pkeys)))
    projections = islice(permutations(sorted(pkeys), gcols), 30)
    return any(gold_sig == _sig(pred_rows, proj) for proj in projections)


TOP_N_TOLERANCE = 20          # 题目约定"结果超 20 条只输出前 20 条"时，宽松匹配接受的行数
LOOSE_ROW_LIMIT = 2000        # 宽松匹配的最大行数（超出行数不做 O(n²) 配对，避免评测过慢）


def _row_values(row) -> list:
    """行 → 值列表（结果为 dict 行，需取 values，避免把列名当值）。"""
    return list(row.values()) if isinstance(row, dict) else list(row)


def _loose_tokens(row) -> set[str]:
    """行的宽松词元集：列值 + 值内分词片段 + 相邻 2~3 列拼接。

    用于容忍表示差异：gold 拆成 first_name/last_name，agent 拼成 "Mike Hillyer"；
    或 agent 多输出 id 等标识列（额外值不影响 gold 值被覆盖）。
    """
    vals = [_norm_value(v) for v in _row_values(row)]
    toks = {v for v in vals if v}
    for s in vals:
        toks |= {p for p in re.split(r"[\s\-_/|、]+", s) if p}
    for i in range(len(vals)):
        for j in range(i + 2, min(len(vals), i + 3) + 1):
            toks.add(" ".join(vals[i:j]))
    return toks


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_NUM_TOL = 0.01              # 数值容差：容忍 ROUND(x, 2)/浮点精度导致的末位差异


def _date_prefix_hit(v: str, toks: set[str]) -> bool:
    """日期粒度差异：gold '2005-05-01' 与 pred '2005-05' 这类同月不同精度视为匹配。"""
    if not re.match(r"\d{4}-\d{2}", v):
        return False
    short = v[:7]
    return any(
        t.startswith(short) or short.startswith(t)
        for t in toks if re.match(r"\d{4}-\d{2}", t)
    )


def _row_covered(gold_row, pred_tokens: set[str]) -> bool:
    """gold 行的每个非空值都能在 pred 行的宽松词元中找到。

    容差：数值允许 _NUM_TOL 偏差（ROUND/浮点精度），日期允许同月不同粒度
    （DATE_TRUNC 的 '2005-05-01' vs to_char 的 '2005-05'）。
    """
    pnums = [float(t) for t in pred_tokens if _NUM_RE.fullmatch(t)]
    for x in _row_values(gold_row):
        v = _norm_value(x)
        if not v or v in pred_tokens:
            continue
        if _NUM_RE.fullmatch(v) and any(abs(float(v) - p) <= _NUM_TOL for p in pnums):
            continue
        if _date_prefix_hit(v, pred_tokens):
            continue
        return False
    return True


def rows_match_relaxed(pred_rows, gold_rows) -> tuple[bool, str]:
    """宽松 EX：gold 值级覆盖（容忍列增删/拼接拆分/行序），支持"只输出前 N 条"。

    与严格匹配的区别是只约束"gold 的每个值必须出现"，允许 agent 附带标识列；
    行数必须一致，或在 gold 超过 TOP_N_TOLERANCE 时 pred 恰为前 N 条子集。
    """
    if not gold_rows or not pred_rows:
        return (not gold_rows and not pred_rows), "empty"
    if len(gold_rows) > LOOSE_ROW_LIMIT or len(pred_rows) > LOOSE_ROW_LIMIT:
        return False, ""

    # 1. 全量等行数：gold 每行都能占用一个未被占用的 pred 行（行序无关）
    if len(pred_rows) == len(gold_rows):
        ptoks = [_loose_tokens(p) for p in pred_rows]
        used = [False] * len(pred_rows)
        for g in gold_rows:
            for i, toks in enumerate(ptoks):
                if not used[i] and _row_covered(g, toks):
                    used[i] = True
                    break
            else:
                return False, ""
        return True, "relaxed"

    # 2. top-N：题目要求只输出前 N 条，pred 每行需覆盖 gold 中一行（不可重复占用）
    want = min(TOP_N_TOLERANCE, len(gold_rows))
    if len(gold_rows) > TOP_N_TOLERANCE and len(pred_rows) == want:
        ptoks = [_loose_tokens(p) for p in pred_rows]
        used = [False] * len(gold_rows)
        for toks in ptoks:
            for i, g in enumerate(gold_rows):
                if not used[i] and _row_covered(g, toks):
                    used[i] = True
                    break
            else:
                return False, ""
        return True, f"top{TOP_N_TOLERANCE}"
    return False, ""


# ============================================================ 业务库执行

async def fetch_rows(dsn: str, sql: str) -> tuple[list, str | None]:
    """执行 SQL 返回 (行, 错误)。错误时行空、错误非空。"""
    conn = await asyncpg.connect(dsn=dsn, timeout=SQL_TIMEOUT_S)
    try:
        rows = await asyncio.wait_for(conn.fetch(sql), SQL_TIMEOUT_S)
        return [tuple(r) for r in rows], None
    except Exception as exc:
        return [], str(exc)
    finally:
        await conn.close()


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

async def _load_sqls(conn: asyncpg.Connection, trace_id: str) -> list[dict]:
    """取一轮 trace 的全部 execute_postgresql 尝试（时序），含 db_name/sql/status。

    语义数据自 tool_semantics 独立表读取（tool_args 入参分列，span_id 关联 span）。
    """
    rows = await conn.fetch(
        """
        SELECT tool_args, success
        FROM telemetry.tool_semantics
        WHERE trace_id=$1 AND tool_name='execute_postgresql'
        ORDER BY created_at
        """, trace_id)
    out = []
    for row in rows:
        args = row["tool_args"] if isinstance(row["tool_args"], dict) else json.loads(row["tool_args"] or "{}")
        sql = str(args.get("sql") or "").strip()
        if sql:
            out.append({
                "sql": sql,
                "db_name": str(args.get("datasource") or args.get("db_name") or ""),
                "status": "ok" if row["success"] else "error",
            })
    return out


def _json_default(v):
    """gold 行 JSON 序列化兜底：Decimal/timedelta 转为与 _norm_value 同口径的数值。"""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, timedelta):
        return v.total_seconds() / 86400
    return str(v)


def _load_gold_cache(case, gold_hash: str) -> tuple[list | None, float]:
    """读 gold 结果缓存（eval_cases）：hash 一致才有效 → (rows, gold 秒)；miss → (None, 0)。"""
    if case["gold_sql_hash"] != gold_hash or case["gold_rows"] is None:
        return None, 0.0
    raw = case["gold_rows"]
    rows = json.loads(raw) if isinstance(raw, str) else raw
    return rows, (case["gold_exec_ms"] or 0.0) / 1000.0


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
                result = await _score_one(conn, cache, trace_id, case)
                await _write_scores(conn, trace_id, case["id"], result)
                n_scored += 1
                print(f"[{result['category'].upper()}] trace={trace_id} EX={result['ex']} VES={result['ves']} {result['category']}")
        finally:
            for c in cache.values():
                await c.close()
        print(f"[DONE] 评分 {n_scored} 条，跳过 {n_skip} 条")
    finally:
        await conn.close()


async def _score_one(conn, cache: dict, trace_id: str, case) -> dict:
    """单条评分：全部成功 SQL 逐条重执行作候选（时序），首个与 gold 匹配者判 EX/VES。

    候选制理由：agent 常在给出最终 SQL 后再做分页取全量/聚合复核等后续执行，
    只看最后一条会把正确答案误判为 mismatch。
    """
    sqls = await _load_sqls(conn, trace_id)
    if not sqls:
        return {"ex": 0.0, "ves": 0.0, "valid": 0.0, "category": "no_sql", "detail": "无 execute_postgresql span"}
    ok_sqls: list[dict] = []
    for s in sqls:  # 去重保持时序
        if s["status"] == "ok" and s["sql"] and s["sql"] not in [c["sql"] for c in ok_sqls]:
            ok_sqls.append(s)
    if not ok_sqls:
        return {"ex": 0.0, "ves": 0.0, "valid": 0.0, "category": "sql_error",
                "detail": f"全部 {len(sqls)} 次 SQL 执行失败"}
    db_name = ok_sqls[-1]["db_name"] or case["db_name"]
    if not db_name:
        return {"ex": 0.0, "ves": 0.0, "valid": 1.0, "category": "db_unknown", "detail": "无法确定目标库"}
    dsn = postgres_business(db_name)
    if db_name not in cache:
        conn_biz = await asyncpg.connect(dsn=dsn, timeout=SQL_TIMEOUT_S)
        # 业务表可能挂在 datasource.schema（如 business.dvdrental），评分连接补 search_path
        schema = next(
            (s.split(".", 1)[1] for s in PUBLIC_SCHEMAS
             if "." in s and s.split(".", 1)[0] == db_name),
            "",
        )
        if schema:
            await conn_biz.execute(f'SET search_path TO "{schema}", public')
        cache[db_name] = conn_biz
    conn_biz = cache[db_name]
    # gold：优先用 eval_cases 缓存（gold_sql hash 一致），miss 才执行并回填（失败不回填，下次重试）
    gold_hash = hashlib.md5(" ".join(case["gold_sql"].split()).encode()).hexdigest()
    gold_rows, gold_time = _load_gold_cache(case, gold_hash)
    if gold_rows is not None:
        gold_err = None
    else:
        t0 = time.perf_counter()
        try:
            gold_rows = [dict(r) for r in await asyncio.wait_for(conn_biz.fetch(case["gold_sql"]), SQL_TIMEOUT_S)]
            gold_time = time.perf_counter() - t0
            gold_err = None
            await conn.execute(
                """
                UPDATE telemetry.eval_cases
                SET gold_rows=$2, gold_exec_ms=$3, gold_sql_hash=$4
                WHERE id=$1
                """, case["id"],
                json.dumps(gold_rows, ensure_ascii=False, default=_json_default),
                gold_time * 1000, gold_hash)
        except Exception as exc:
            gold_rows, gold_err, gold_time = [], str(exc), time.perf_counter() - t0
    if gold_err:
        return {"ex": 0.0, "ves": 0.0, "valid": 1.0, "category": "gold_error", "detail": f"金标准 SQL 执行失败: {gold_err[:300]}"}
    gold_dep = _depaginate(case["gold_sql"])
    # 候选逐条重执行，首个匹配即成功
    n_valid = len(ok_sqls)
    last_err = ""
    last_rows = 0
    for cand in ok_sqls:
        # information_schema 探查类 SQL 不会匹配 gold，跳过重执行省时
        low = cand["sql"].lower()
        if "information_schema" in low or low.lstrip().startswith("select column_name"):
            continue
        t0 = time.perf_counter()
        try:
            pred_rows = [dict(r) for r in await asyncio.wait_for(conn_biz.fetch(cand["sql"]), SQL_TIMEOUT_S)]
            pred_time = time.perf_counter() - t0
        except Exception as exc:
            last_err = str(exc)
            continue
        last_rows = len(pred_rows)
        if rows_match(pred_rows, gold_rows):
            ves = min(1.0, gold_time / pred_time) if pred_time > 0 else 0.0
            return {"ex": 1.0, "ves": ves, "valid": 1.0, "category": "ok", "detail": ""}
        # 严格匹配失败 → 宽松匹配（容忍列增删、姓名拼接拆分、只输出前 N 条）
        hit, how = rows_match_relaxed(pred_rows, gold_rows)
        if hit:
            ves = min(1.0, gold_time / pred_time) if pred_time > 0 else 0.0
            return {"ex": 1.0, "ves": ves, "valid": 1.0, "category": "ok_relaxed",
                    "detail": f"表示等价（{how}）：列/拼接差异不影响结果一致性"}
        # top-N 并列：任一侧带 LIMIT 都去 LIMIT 比全序——gold 带 LIMIT 时并列边界
        # 不稳定；agent 带 LIMIT 时只是分页截断展示（如先 count 全量再分页取数）
        cand_dep = _depaginate(cand["sql"])
        if gold_dep or cand_dep:
            cand_dep = cand_dep or cand["sql"]
            try:
                pred2 = [dict(r) for r in await asyncio.wait_for(conn_biz.fetch(cand_dep), SQL_TIMEOUT_S)]
                if gold_dep:
                    gold2 = [dict(r) for r in await asyncio.wait_for(conn_biz.fetch(gold_dep), SQL_TIMEOUT_S)]
                else:
                    gold2 = gold_rows  # gold 本就无 LIMIT，免重执行
                if rows_match(pred2, gold2):
                    return {"ex": 1.0, "ves": min(1.0, gold_time / pred_time) if pred_time > 0 else 0.0,
                            "valid": 1.0, "category": "ok", "detail": "top-n tie（去 LIMIT 全序匹配）"}
                hit2, how2 = rows_match_relaxed(pred2, gold2)
                if hit2:
                    return {"ex": 1.0, "ves": min(1.0, gold_time / pred_time) if pred_time > 0 else 0.0,
                            "valid": 1.0, "category": "ok_relaxed",
                            "detail": f"表示等价（{how2}）：去 LIMIT 全序匹配"}
            except Exception as exc:
                last_err = str(exc)
    return {"ex": 0.0, "ves": 0.0, "valid": 1.0 if n_valid else 0.0,
            "category": "result_mismatch",
            "detail": f"候选 {n_valid} 条无一匹配；最后执行 {last_rows} 行 vs gold {len(gold_rows)} 行{('; ' + last_err[:200]) if last_err else ''}"}


async def _write_scores(conn: asyncpg.Connection, trace_id: str, case_id: int, result: dict) -> None:
    """写回三类评分（同一 trace 重评时先清旧，保证一份最新结果）。"""
    await conn.execute("DELETE FROM telemetry.trace_scores WHERE trace_id=$1 AND evaluator='eval_script'", trace_id)
    rows = [
        (trace_id, "EX", result["ex"], result["category"], "eval_script", case_id),
        (trace_id, "VES", result["ves"], "", "eval_script", case_id),
        (trace_id, "valid_sql", result["valid"], "", "eval_script", case_id),
    ]
    if result["detail"]:
        rows.append((trace_id, "detail", None, result["detail"][:1000], "eval_script", case_id))
    await conn.executemany(
        """
        INSERT INTO telemetry.trace_scores (trace_id, name, value, comment, evaluator, case_id)
        VALUES ($1,$2,$3,$4,$5,$6)
        """, rows)


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
        args = r["tool_args"] if isinstance(r["tool_args"], dict) else json.loads(r["tool_args"] or "{}")
        res = r["tool_result"] if isinstance(r["tool_result"], dict) else json.loads(r["tool_result"] or "{}")
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
        out = sp["output"] if isinstance(sp["output"], dict) else json.loads(sp["output"] or "{}")
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
        out = sp["output"] if isinstance(sp["output"], dict) else json.loads(sp["output"] or "{}")
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

        # ── 组装报告 ──
        lines = [
            "# 评测报告", "",
            f"- 评测范围：{since.astimezone():%Y-%m-%d %H:%M:%S}（本机时间）之后",
            f"- **评测题数**：{len(cases_scored)} 题（共 {n} 条 trace 记录）",
            f"- **EX**：{ex:.2%}" if ex is not None else "- EX：无数据",
            f"- **Valid SQL Rate**：{valid:.2%}" if valid is not None else "- Valid SQL Rate：无数据",
            f"- **VES**：{ves:.3f}" if ves is not None else "- VES：无数据",
            "",
            "## 题目概览", "",
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
            SELECT t.trace_id, t.status,
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

        # 文件名：用户显式 --out 则尊重，否则自动加时间戳，默认放 eval/report/ 子目录
        if args.out:
            out = Path(args.out)
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_dir = _REPO / "eval" / "report"
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
    p.add_argument("--out", default="", help="输出路径（默认 eval/report_YYYYMMDD_HHMMSS.md）")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
