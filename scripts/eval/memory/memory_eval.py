"""memory 效果评测（P1-5）：20 条样本，三类指标。

指标（写入报告，作为 P0-2 评测的一个维度）：
1. 跨 session 追问命中率（10 条）：session A 写入事实 → session B 追问 →
   warm/hot 召回结果含期望关键词即命中；
2. 污染率（5 条）：写入干扰性事实后追问无关问题 → 召回中出现不相关记忆的比例
   （越低越好，衡量"宁缺毋滥"门控）；
3. 一致性（5 条）：同 slot_key 重复写入（措辞不同）→ hot 层 uk_slot 幂等，
   每个槽位 active 记录应唯一。

运行前置：宿主机 PG（memory schema）与 embedding 服务可用；在 backend 目录下：
    .venv\\Scripts\\python.exe ..\\scripts\\eval\\memory\\memory_eval.py
产出：scripts/eval/report/memory_eval_YYYYMMDD_HHMMSS.md（不自动清理评测数据，
    全部数据带 session_id 前缀 meval_，可手工按前缀删除）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[3] / "backend"
os.chdir(_BACKEND)  # config.json / .env 按运行目录加载
sys.path.insert(0, str(_BACKEND / "src"))

from codegenx.ai_service.memory.memory_store import list_memories  # noqa: E402
from codegenx.ai_service.memory.retriever import search_warm  # noqa: E402
from codegenx.ai_service.memory.store_init import ensure_memory_schema, get_memory_pool  # noqa: E402
from codegenx.ai_service.memory.writer import write_memories  # noqa: E402
from codegenx.ai_service.utils.config import config  # noqa: E402

EVAL_USER = "eval_user"
EVAL_APP = "eval_app"
RUN_TAG = f"meval_{time.strftime('%Y%m%d_%H%M%S')}"

# ── ① 跨 session 追问命中率样本（fact 写入 session A；query+expect 在 session B 追问）──
HIT_SAMPLES = [
    {"fact": "用户偏好先看结论再看数据明细，报表默认按月份聚合。", "memory_type": "user_preference",
     "query": "报表应该怎么展示比较合适？", "expect": "按月份聚合"},
    {"fact": "业务口径：GMV 统计不包含已取消订单。", "memory_type": "hard_constraint",
     "query": "帮我算一下 GMV", "expect": "已取消订单"},
    {"fact": "公司主营电商平台，核心业务表是订单表 orders 与用户表 users。", "memory_type": "identity_fact",
     "query": "我们公司是做什么的？", "expect": "电商"},
    {"fact": "团队规范：所有 SQL 必须带 LIMIT 防止全表扫。", "memory_type": "system_rule",
     "query": "写查询要注意什么？", "expect": "LIMIT"},
    {"fact": "数据看板地址在 https://bi.example.com/dashboard。", "memory_type": "external_resource",
     "query": "去哪里看现成的看板？", "expect": "bi.example.com"},
    {"fact": "用户习惯用中文提问，回复请用中文。", "memory_type": "user_preference",
     "query": "用什么语言回复我？", "expect": "中文"},
    {"fact": "分析口径：月活跃用户按去重 user_id 计数。", "memory_type": "hard_constraint",
     "query": "月活怎么统计？", "expect": "去重"},
    {"fact": "销售目标：2026 年 Q1 目标 500 万。", "memory_type": "identity_fact",
     "query": "这季度目标多少？", "expect": "500 万"},
    {"fact": "用户偏好图表用柱状图而不是饼图。", "memory_type": "user_preference",
     "query": "图表选什么类型好？", "expect": "柱状图"},
    {"fact": "敏感字段（手机号/身份证）输出必须脱敏。", "memory_type": "system_rule",
     "query": "涉及手机号的数据怎么处理？", "expect": "脱敏"},
]

# ── ② 污染率样本（写入干扰事实；query 与干扰事实无关，期望不召回）──────────────
POLLUTION_SAMPLES = [
    {"fact": "用户所在地是上海。", "memory_type": "identity_fact",
     "query": "2026 年 Q1 的 GMV 目标是多少？", "expect_none_of": "上海"},
    {"fact": "团队负责人是张三。", "memory_type": "identity_fact",
     "query": "数据看板地址是什么？", "expect_none_of": "张三"},
    {"fact": "用户不喜欢饼图。", "memory_type": "user_preference",
     "query": "公司主营什么业务？", "expect_none_of": "饼图"},
    {"fact": "历史项目是库存分析。", "memory_type": "identity_fact",
     "query": "月活怎么统计？", "expect_none_of": "库存"},
    {"fact": "用户习惯早上开会。", "memory_type": "user_preference",
     "query": "SQL 要注意什么规范？", "expect_none_of": "开会"},
]

# ── ③ 一致性样本（同 slot_key 重复写入，措辞不同，期望 active 唯一）────────────
CONSISTENCY_SAMPLES = [
    {"slot_key": "pref:language", "writes": ["用户偏好中文回复。", "回复语言请用中文。"],
     "memory_type": "user_preference"},
    {"slot_key": "rule:limit", "writes": ["SQL 必须带 LIMIT。", "所有查询记得加 LIMIT 限制行数。"],
     "memory_type": "system_rule"},
    {"slot_key": "fact:gmv", "writes": ["GMV 不含已取消订单。", "取消订单不计入 GMV 口径。"],
     "memory_type": "hard_constraint"},
    {"slot_key": "pref:chart", "writes": ["图表偏好柱状图。", "优先使用柱状图展示。"],
     "memory_type": "user_preference"},
    {"slot_key": "fact:goal", "writes": ["Q1 目标 500 万。", "一季度销售目标为五百万。"],
     "memory_type": "identity_fact"},
]


async def _recall(app_id: str, user_id: str, query: str) -> list:
    """按 query 检索 warm 召回（无 hot 全量干扰，单独衡量追问命中）。"""
    return await search_warm(app_id, user_id, query)


async def run_hit_samples() -> dict:
    """① 跨 session 追问命中率。"""
    sid_a, sid_b = f"{RUN_TAG}_hit_A", f"{RUN_TAG}_hit_B"
    for i, s in enumerate(HIT_SAMPLES):
        await write_memories(EVAL_USER, EVAL_APP, f"{sid_a}_{i}",
                             [{"content": s["fact"], "memory_type": s["memory_type"],
                               "confidence": 0.9, "source_type": 1}])
    await asyncio.sleep(0.5)  # 等向量落库可见
    hits, details = 0, []
    for s in HIT_SAMPLES:
        entries = await _recall(EVAL_APP, EVAL_USER, s["query"])
        matched = any(s["expect"] in (e.summary or e.content or "") for e in entries)
        hits += 1 if matched else 0
        details.append({"query": s["query"], "expect": s["expect"],
                        "recall": len(entries), "hit": matched})
    return {"name": "跨 session 追问命中率", "total": len(HIT_SAMPLES), "hits": hits,
            "rate": round(hits / len(HIT_SAMPLES), 2), "details": details}


async def run_pollution_samples() -> dict:
    """② 污染率：无关追问召回干扰事实的比例。"""
    for i, s in enumerate(POLLUTION_SAMPLES):
        await write_memories(EVAL_USER, EVAL_APP, f"{RUN_TAG}_pol_{i}",
                             [{"content": s["fact"], "memory_type": s["memory_type"],
                               "confidence": 0.9, "source_type": 1}])
    await asyncio.sleep(0.5)
    polluted, details = 0, []
    for s in POLLUTION_SAMPLES:
        entries = await _recall(EVAL_APP, EVAL_USER, s["query"])
        bad = [e for e in entries if s["expect_none_of"] in (e.summary or e.content or "")]
        polluted += 1 if bad else 0
        details.append({"query": s["query"], "polluted": bool(bad), "recall": len(entries)})
    return {"name": "污染率（无关追问误召回）", "total": len(POLLUTION_SAMPLES),
            "polluted": polluted, "rate": round(polluted / len(POLLUTION_SAMPLES), 2),
            "details": details}


async def run_consistency_samples() -> dict:
    """③ 一致性：同 slot_key 重复写入 → active 记录唯一（uk_slot 幂等）。"""
    from codegenx.ai_service.memory.models import STATUS_ACTIVE
    for i, s in enumerate(CONSISTENCY_SAMPLES):
        for w in s["writes"]:
            await write_memories(EVAL_USER, EVAL_APP, f"{RUN_TAG}_cons_{i}",
                                 [{"content": w, "memory_type": s["memory_type"],
                                   "slot_key": s["slot_key"], "confidence": 0.9,
                                   "source_type": 1}])
    await asyncio.sleep(0.5)
    consistent, details = 0, []
    for s in CONSISTENCY_SAMPLES:
        rows = await list_memories(EVAL_APP, EVAL_USER, slot_key=s["slot_key"]) \
            if _supports_slot_filter() else await list_memories(EVAL_APP, EVAL_USER)
        active = [r for r in rows
                  if getattr(r, "slot_key", "") == s["slot_key"]
                  and getattr(r, "status", STATUS_ACTIVE) == STATUS_ACTIVE]
        ok = len(active) == 1
        consistent += 1 if ok else 0
        details.append({"slot_key": s["slot_key"], "active_count": len(active), "ok": ok})
    return {"name": "重复写入一致性（uk_slot 幂等）", "total": len(CONSISTENCY_SAMPLES),
            "consistent": consistent, "rate": round(consistent / len(CONSISTENCY_SAMPLES), 2),
            "details": details}


def _supports_slot_filter() -> bool:
    """list_memories 是否支持按 slot_key 过滤（签名探测，避免硬编码）。"""
    import inspect
    from codegenx.ai_service.memory import memory_store
    return "slot_key" in inspect.signature(memory_store.list_memories).parameters


async def main() -> int:
    assert config.memory.enabled, "memory 未启用（config.json#memory.enabled），无法评测"
    await get_memory_pool()
    await ensure_memory_schema()

    hit = await run_hit_samples()
    pollution = await run_pollution_samples()
    consistency = await run_consistency_samples()

    # ── 报告落盘 ──
    report_dir = Path(__file__).resolve().parents[1] / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"memory_eval_{time.strftime('%Y%m%d_%H%M%S')}.md"
    lines = [
        "# memory 效果评测报告（P1-5）",
        f"- run_tag: `{RUN_TAG}`（数据按此前缀落库，手工清理按前缀删除）",
        "",
        "| 指标 | 总数 | 结果 | 比率 |",
        "| --- | --- | --- | --- |",
        f"| {hit['name']} | {hit['total']} | 命中 {hit['hits']} | {hit['rate']:.0%} |",
        f"| {pollution['name']} | {pollution['total']} | 污染 {pollution['polluted']} | {pollution['rate']:.0%} |",
        f"| {consistency['name']} | {consistency['total']} | 一致 {consistency['consistent']} | {consistency['rate']:.0%} |",
        "",
        "## ① 追问命中明细",
        "| query | 期望关键词 | 召回数 | 命中 |",
        "| --- | --- | --- | --- |",
        *[f"| {d['query']} | {d['expect']} | {d['recall']} | {'✅' if d['hit'] else '❌'} |"
          for d in hit["details"]],
        "",
        "## ② 污染明细",
        "| query | 干扰词 | 召回数 | 污染 |",
        "| --- | --- | --- | --- |",
        *[f"| {d['query']} | {s['expect_none_of']} | {d['recall']} | {'⚠️' if d['polluted'] else '✅'} |"
          for s, d in zip(POLLUTION_SAMPLES, pollution["details"])],
        "",
        "## ③ 一致性明细",
        "| slot_key | active 记录数 | 结果 |",
        "| --- | --- | --- |",
        *[f"| {d['slot_key']} | {d['active_count']} | {'✅' if d['ok'] else '❌'} |"
          for d in consistency["details"]],
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[memory_eval] 报告已生成: {report_path}")
    print(f"[memory_eval] 命中率 {hit['rate']:.0%} | 污染率 {pollution['rate']:.0%} | 一致性 {consistency['rate']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
