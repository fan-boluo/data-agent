-- ============================================================
-- 评测运行（eval run）落库（PostgreSQL，schema=telemetry）
-- 定位：把 ai_service/eval/eval.py 的"命令行四步"变成服务端可触发、可查进度、
--       可对比基线的评测平台（对齐语思"智能体评估"形态）
-- 依赖：init/telemetry_schema.sql（eval_datasets / eval_cases / traces / trace_scores）
-- 使用：psql -f init/eval_schema.sql
--
-- 约定：
--   1. 题集复用 telemetry.eval_datasets / eval_cases，不重复建表
--   2. 一次 run = 一批题的重放 + 评分 + 汇总；逐题为一行 items
--   3. 指标口径与 eval.py 一致（EX / VES / valid_sql），其余维度从 trace 派生
-- ============================================================

CREATE SCHEMA IF NOT EXISTS telemetry;

-- ------------------------------------------------------------
-- 1. eval_runs：一次评测运行（元信息 + 进度 + 汇总指标）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry.eval_runs (
  id              BIGSERIAL    PRIMARY KEY,
  dataset_id      BIGINT       NOT NULL REFERENCES telemetry.eval_datasets(id),
  name            TEXT         NOT NULL DEFAULT '',   -- 运行名（默认 数据集-时间戳）
  status          TEXT         NOT NULL DEFAULT 'pending',  -- pending/running/completed/failed/cancelled
  source          TEXT         NOT NULL DEFAULT 'api',      -- api / cli
  baseline_run_id BIGINT       REFERENCES telemetry.eval_runs(id) ON DELETE SET NULL,  -- 对比基线
  limit_count     INTEGER      NOT NULL DEFAULT 0,    -- 本次最多跑几题，0=全量
  case_total      INTEGER      NOT NULL DEFAULT 0,
  case_done       INTEGER      NOT NULL DEFAULT 0,
  case_failed     INTEGER      NOT NULL DEFAULT 0,
  metrics         JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- 6 维汇总 + 失败归类
  report_md       TEXT,                                -- 汇总报告（Markdown，失败归因）
  error           TEXT,
  started_at      TIMESTAMPTZ,
  ended_at        TIMESTAMPTZ,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE telemetry.eval_runs IS '一次评测运行：题集快照、进度、6 维汇总指标与 Markdown 报告';
CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON telemetry.eval_runs (dataset_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status  ON telemetry.eval_runs (status, created_at DESC);

-- ------------------------------------------------------------
-- 2. eval_run_items：run 内逐题结果（case × run）
--    评分列与 trace_scores 同口径，另加 trace 派生维度（步数/耗时/工具失败/上下文）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry.eval_run_items (
  id             BIGSERIAL    PRIMARY KEY,
  run_id         BIGINT       NOT NULL REFERENCES telemetry.eval_runs(id) ON DELETE CASCADE,
  case_id        BIGINT       NOT NULL REFERENCES telemetry.eval_cases(id),
  trace_id       TEXT,                                  -- 重放产生的 trace（失败可空）
  status         TEXT         NOT NULL DEFAULT 'pending', -- pending/running/done/failed
  ex             DOUBLE PRECISION,
  ves            DOUBLE PRECISION,
  valid_sql      DOUBLE PRECISION,
  category       TEXT         NOT NULL DEFAULT '',       -- ok/ok_relaxed/no_sql/sql_error/result_mismatch/gold_error/db_unknown
  detail         TEXT,                                   -- 失败细节（截断）
  steps          INTEGER,                                -- llm span 数（agent 决策步数）
  tool_calls     INTEGER,                                -- 工具调用次数
  tool_failures  INTEGER,                                -- 工具失败次数
  context_usage  DOUBLE PRECISION,                       -- 上下文占用峰值（0~1）
  duration_ms    DOUBLE PRECISION,                       -- 一轮请求耗时
  error          TEXT,                                   -- 重放/评分阶段异常
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  UNIQUE (run_id, case_id)
);
COMMENT ON TABLE telemetry.eval_run_items IS '评测逐题结果：EX/VES/valid_sql + trace 派生的步数、耗时、工具失败率、上下文占用';
CREATE INDEX IF NOT EXISTS idx_eval_items_run ON telemetry.eval_run_items (run_id, status);
