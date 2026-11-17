"""评测平台管理端 API（/api/admin/eval/*，管理员）。

能力（自定义标准题集评测的最小闭环，整体评测体系待重新规划）：
  ① GET  /datasets            题集列表（题数/已校验数）
  ② POST /runs                触发一次评测（异步执行，立即返回 runId）
  ③ GET  /runs                历史运行列表（榜单/对比入口）
  ④ GET  /runs/{id}           运行详情：进度 + 6 维指标 + 逐题结果 + Markdown 报告
  ⑤ GET  /runs/{id}/compare   与 baseline run 的多维度对比（含逐题回归/修复）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from codegenx.ai_service.eval import runner, store
from codegenx.ai_service.utils.config import config
from codegenx.gateway.middleware.auth import require_role
from codegenx.user_service.user_enums import UserRole
from shared.exceptions.business_exception import BusinessException
from shared.exceptions.error_code import ErrorCode
from shared.utils.result_utils import success

eval_router = APIRouter(
    prefix="/admin/eval",
    tags=["eval-admin"],
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)

# 对比时逐题展示的指标（值越大越好，"缺数据"用 None 表示不比较）
_COMPARE_METRICS = ("ex", "ves", "valid_sql", "tool_success_rate", "avg_steps", "avg_duration_ms")


class RunCreateRequest(BaseModel):
    """触发评测请求：datasetId 与 datasetName 二选一，limit=0 为全量。"""

    dataset_id: int | None = Field(default=None, alias="datasetId")
    dataset_name: str | None = Field(default=None, alias="datasetName")
    name: str = ""
    baseline_run_id: int | None = Field(default=None, alias="baselineRunId")
    limit: int = Field(default=0, ge=0)


@eval_router.get("/datasets")
async def list_datasets():
    return success(data=await store.list_datasets())


@eval_router.post("/runs")
async def create_run(payload: RunCreateRequest):
    """触发评测：建 run → 后台执行（重放+评分），立即返回 runId 供前端轮询。"""
    if not config.eval.enabled:
        raise BusinessException(ErrorCode.FORBIDDEN_ERROR, "评测功能已关闭（待重新规划）")
    dataset = await store.get_dataset(payload.dataset_id, payload.dataset_name)
    if not dataset:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "题集不存在")
    if runner.running_run_id() is not None:
        raise BusinessException(ErrorCode.FORBIDDEN_ERROR, "已有评测运行中，请等待完成")
    run = await store.create_run(
        dataset_id=dataset["id"], name=payload.name.strip(),
        baseline_run_id=payload.baseline_run_id, limit_count=payload.limit,
    )
    try:
        runner.launch(run["id"])
    except runner.EvalBusyError as exc:
        await store.set_run(run["id"], status=store.RUN_STATUS_FAILED, error=str(exc))
        raise BusinessException(ErrorCode.FORBIDDEN_ERROR, str(exc)) from exc
    return success(data={"runId": run["id"], "name": run["name"], "datasetId": dataset["id"]})


@eval_router.get("/runs")
async def list_runs(
    dataset_id: int | None = Query(default=None, alias="datasetId"),
    limit: int = Query(default=20, ge=1, le=200),
):
    return success(data=await store.list_runs(dataset_id, limit))


@eval_router.get("/runs/{run_id}")
async def get_run(run_id: int):
    """运行详情：进度、6 维指标、逐题结果（含题面）、Markdown 报告。"""
    run = await store.get_run(run_id)
    if not run:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "评测运行不存在")
    items = await store.list_items(run_id)
    return success(data={"run": run, "items": items})


@eval_router.get("/runs/{run_id}/compare")
async def compare_run(run_id: int):
    """与基线 run 对比：整体指标差值 + 逐题修复/回归。"""
    run = await store.get_run(run_id)
    if not run:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "评测运行不存在")
    baseline_id = run.get("baseline_run_id")
    if not baseline_id:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "该运行未指定基线")
    baseline = await store.get_run(baseline_id)
    if not baseline:
        raise BusinessException(ErrorCode.PARAMS_ERROR, "基线运行不存在")
    cur_metrics = run.get("metrics") or {}
    base_metrics = baseline.get("metrics") or {}
    metrics = {
        key: {"current": cur_metrics.get(key), "baseline": base_metrics.get(key)}
        for key in _COMPARE_METRICS
    }
    cur_items = {i["case_id"]: i for i in await store.list_items(run_id, limit=10_000)}
    base_items = {i["case_id"]: i for i in await store.list_items(baseline_id, limit=10_000)}
    fixed, broken = [], []
    for case_id, item in cur_items.items():
        old = base_items.get(case_id)
        if old is None or old.get("ex") is None or item.get("ex") is None:
            continue
        pair = {"caseId": case_id, "question": item.get("question"),
                "baselineEx": old["ex"], "currentEx": item["ex"], "category": item.get("category")}
        if old["ex"] == 0 and item["ex"] == 1:
            fixed.append(pair)
        elif old["ex"] == 1 and item["ex"] == 0:
            broken.append(pair)
    return success(data={
        "runId": run_id, "baselineRunId": baseline_id, "metrics": metrics,
        "fixed": fixed, "broken": broken,
    })
