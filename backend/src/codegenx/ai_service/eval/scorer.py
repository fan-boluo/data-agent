"""评测评分器（EX / VES / valid_sql）——服务端与 CLI 共用的唯一真源。

从 ai_service/eval/eval.py 抽出：结果归一化对比（严格 + 宽松）、gold 结果缓存、
单题打分与评分写回。CLI 与服务端 runner 都 import 本模块，避免两套判定标准。

判定口径：
  EX        预测结果与 gold 结果集一致（严格列投影匹配，失败后退宽松值覆盖匹配）
  VES       有效执行效率 = min(1, gold 耗时 / 预测耗时)
  valid_sql 至少有一条 SQL 成功执行（语法/权限有效）
失败归类（category）：no_sql / sql_error / db_unknown / gold_error / result_mismatch
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import islice, permutations

import asyncpg

from db.postgres.postgres_client import postgres_business
from shared.constants import PUBLIC_SCHEMAS

SQL_TIMEOUT_S = 30            # 业务库单条 SQL 执行超时
TRACE_WAIT_S = 15             # 重放后等 trace 落库超时
TOP_N_TOLERANCE = 20          # 题目约定"结果超 20 条只输出前 20 条"时，宽松匹配接受的行数
LOOSE_ROW_LIMIT = 2000        # 宽松匹配的最大行数（超出行数不做 O(n²) 配对，避免评测过慢）

_NULL_VALUES = {"", "null", "none", "nan", "nat", "-"}


# ============================================================ 结果对比（EX 核心）

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
    gcols, pcols = len(gkeys), len(pkeys)
    if gcols > pcols:
        return False
    gold_sig = _sig(gold_rows, tuple(sorted(gkeys)))
    if pcols > 6:
        return gold_sig == _sig(pred_rows, tuple(sorted(pkeys)))
    projections = islice(permutations(sorted(pkeys), gcols), 30)
    return any(gold_sig == _sig(pred_rows, proj) for proj in projections)


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


async def score_case(conn, cache: dict, trace_id: str, case) -> dict:
    """单条评分：全部成功 SQL 逐条重执行作候选（时序），首个与 gold 匹配者判 EX/VES。

    候选制理由：agent 常在给出最终 SQL 后再做分页取全量/聚合复核等后续执行，
    只看最后一条会把正确答案误判为 mismatch。
    cache：db_name → 业务库连接（一次 run 内复用）。
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


async def write_scores(conn: asyncpg.Connection, trace_id: str, case_id: int, result: dict) -> None:
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
