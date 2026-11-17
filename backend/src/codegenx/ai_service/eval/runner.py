"""评测执行器：API 触发 → 逐题重放（内部 HTTP 调 chat）→ 评分 → 汇总。

与 ai_service/eval/eval.py 的关系：
  - 判定逻辑完全复用 eval.scorer（同一份 EX/VES/valid_sql 口径）；
  - 本模块只负责"服务端编排"：登录、重放、等 trace 落库、进度回写、指标汇总。
单实例内同时只允许一个 run 在跑（评测会打满 LLM 配额，串行避免互相干扰）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import asyncpg
import httpx

from codegenx.ai_service.eval import scorer, store
from codegenx.ai_service.utils.config import config
from db.postgres.postgres_client import postgres_dsn
from shared import log
from shared.config.config import get_settings
from shared.constants import PUBLIC_SCHEMAS

EVAL_PREFIX = "eval"          # 重放 trace_id 前缀（与 CLI 保持一致，score 可复用）
_tasks: dict[int, asyncio.Task] = {}


class EvalBusyError(RuntimeError):
    """已有评测在跑（单实例串行）。"""


def running_run_id() -> int | None:
    """当前正在跑的 run id（无则 None）。"""
    for run_id, task in list(_tasks.items()):
        if not task.done():
            return run_id
        _tasks.pop(run_id, None)
    return None


def launch(run_id: int) -> None:
    """后台拉起评测任务（同一事件循环内执行，任务表防 GC）。"""
    if running_run_id() is not None:
        raise EvalBusyError("已有评测运行中，请等待完成或稍后重试")
    task = asyncio.create_task(_execute(run_id))
    _tasks[run_id] = task
    task.add_done_callback(lambda t: _tasks.pop(run_id, None))


async def _login(base_url: str, account: str, password: str) -> str:
    """以管理员账号登录取 JWT（评测与真实用户走同一入口）。"""
    async with httpx.AsyncClient(base_url=base_url, timeout=15, trust_env=False) as client:
        resp = await client.post("/api/user/login", json={
            "userAccount": account, "userPassword": password})
        resp.raise_for_status()
        body = resp.json()
        token = body.get("data") or ""
        if not token:
            raise RuntimeError(f"评测登录失败: {body}")
        return token


async def _wait_trace(conn: asyncpg.Connection, trace_id: str) -> bool:
    """轮询等 turn_end 落库（trace 异步 flush，落库后才可评分）。"""
    deadline = time.time() + scorer.TRACE_WAIT_S
    while time.time() < deadline:
        if await conn.fetchval("SELECT 1 FROM telemetry.traces WHERE trace_id=$1", trace_id):
            return True
        await asyncio.sleep(0.5)
    return False


def _payload(case: dict, app_id: str, trace_id: str) -> dict:
    """构造 chat 请求（trace/session 编码 case，便于评分与回放关联）。"""
    payload = {
        "appId": app_id, "message": case["question"],
        "sessionId": f"evalsess-{trace_id}",
        "traceId": trace_id, "requestId": f"evalreq-{trace_id}",
        "clientVersion": "eval-service",
    }
    db_name = case.get("db_name") or ""
    if db_name:
        payload["dbName"] = db_name
        # agent 只聚焦 currentDb（datasource.schema），由 db_name 反查白名单条目
        current_db = next((s for s in PUBLIC_SCHEMAS if s.split(".", 1)[0] == db_name), None)
        if current_db:
            payload["currentDb"] = current_db
    return payload


async def _replay(client: httpx.AsyncClient, headers: dict, payload: dict) -> str | None:
    """重放一题：SSE 读到流结束即 turn 结束；返回错误文本（成功为 None）。"""
    async with client.stream("POST", "/api/ai/chat/gen", json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {(await resp.aread())[:200]}"
        async for _ in resp.aiter_text():
            pass
    return None


async def _execute(run_id: int) -> None:
    """run 主流程：取题 → 逐题重放+评分 → 汇总指标与报告。"""
    cfg = config.eval_config
    settings = get_settings()
    meta = await asyncpg.connect(dsn=postgres_dsn())
    cache: dict[str, asyncpg.Connection] = {}
    try:
        run = await store.get_run(run_id)
        if not run:
            log.warning("eval run 不存在: {}", run_id)
            return
        cases = await store.list_cases(run["dataset_id"], run["limit_count"])
        await store.start_run(run_id, len(cases))
        await store.ensure_items(run_id, [c["id"] for c in cases])
        log.info("eval run {} 启动，共 {} 题", run_id, len(cases))
        base_url = cfg.base_url or f"http://127.0.0.1:{settings.app_port}"
        token = await _login(base_url, cfg.account, cfg.password)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(base_url=base_url, timeout=cfg.timeout_seconds,
                                     trust_env=False) as client:
            for case in cases:
                trace_id = f"{EVAL_PREFIX}-{case['id']}-{uuid.uuid4().hex[:8]}"
                await store.update_item(run_id, case["id"], status=store.ITEM_RUNNING, trace_id=trace_id)
                started = time.perf_counter()
                try:
                    error = await _replay(client, headers, _payload(case, cfg.app_id, trace_id))
                    if error:
                        raise RuntimeError(error)
                    if not await _wait_trace(meta, trace_id):
                        raise RuntimeError("trace 未落库（重放超时）")
                    result = await scorer.score_case(meta, cache, trace_id, case)
                    await scorer.write_scores(meta, trace_id, case["id"], result)
                    stats = await store.trace_stats(trace_id)
                    await store.update_item(
                        run_id, case["id"], status=store.ITEM_DONE,
                        ex=result["ex"], ves=result["ves"], valid_sql=result["valid"],
                        category=result["category"], detail=(result.get("detail") or "")[:1000],
                        steps=stats.get("steps"), tool_calls=stats.get("tool_calls"),
                        tool_failures=stats.get("tool_failures"),
                        context_usage=stats.get("context_usage"), duration_ms=stats.get("duration_ms"),
                    )
                    await store.bump_progress(run_id, done=1)
                    log.info("eval case {} {} EX={} VES={} {:.1f}s",
                             case["id"], result["category"], result["ex"],
                             round(result["ves"], 3), time.perf_counter() - started)
                except Exception as exc:  # noqa: BLE001 — 单题失败不中断整轮
                    log.error("eval case {} 失败: {}", case["id"], exc)
                    await store.update_item(
                        run_id, case["id"], status=store.ITEM_FAILED, error=str(exc)[:1000],
                        duration_ms=round((time.perf_counter() - started) * 1000, 1))
                    await store.bump_progress(run_id, failed=1)
                if cfg.interval_seconds:
                    await asyncio.sleep(cfg.interval_seconds)
        items = await store.list_items(run_id, limit=10_000)
        metrics = store.aggregate(items)
        run = await store.get_run(run_id) or run
        run["status"] = store.RUN_STATUS_COMPLETED  # 报告按完成态渲染
        await store.set_run(run_id, status=store.RUN_STATUS_COMPLETED, metrics=metrics,
                            report_md=store.render_report(run, items, metrics),
                            ended_at=datetime.now(timezone.utc))
        log.info("eval run {} 完成: EX={} VES={}", run_id, metrics.get("ex"), metrics.get("ves"))
    except Exception as exc:  # noqa: BLE001 — run 级失败落库，前端可查原因
        log.error("eval run {} 异常终止: {}", run_id, exc)
        try:
            await store.set_run(run_id, status=store.RUN_STATUS_FAILED, error=str(exc)[:2000],
                                ended_at=datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            pass
    finally:
        for conn in cache.values():
            await conn.close()
        await meta.close()
