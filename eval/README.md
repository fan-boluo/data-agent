# 评测流水线（eval/）

基于 **读库模式** 的 Text2SQL 问答质量批量评测。后端走真实 chat 链路（leader 路由、hook、trace 全在），每题独立会话无状态，可复现、可归因。

## 目录结构

```
eval/
├── eval.py                      # 评测闭环脚本（import-cases / replay / score / report）
├── retag_cases.py               # 自动重打标签（根据 gold_sql 语法推断 ops/grain）
├── tags_schema.md               # 固化标签体系（所有评测题目必须从此枚举取值）
├── report/                      # 评测报告输出目录（自动带时间戳命名）
├── dvdrental/question.json      # dvd rental 题集（46 题）
├── tpcds_sf1/
│   ├── tpcds_eval_sf1_final.json  # TPC-DS SF1 题集（82 题，固化标签）
│   └── tpcds_eval_sf1_v1.json     # 旧版（保留对比）
└── README.md                    # 本文件
```

## 完整流程

```
打标签 → 导入题集 → 重放 → 评分 → 报告
(retag)   (import)   (replay) (score) (report)
```

### 0. 固化标签（retag_cases.py）

所有题目标签必须从 [tags_schema.md](./tags_schema.md) 枚举取值。**新增题集或题集标签需重定时先跑 retag**：

```bash
# 重打 dvdrental 标签
python eval/retag_cases.py --dataset dvdrental

# 重打 tpcds_sf1 标签
python eval/retag_cases.py --dataset tpcds_sf1

# 两个都重打
python eval/retag_cases.py --dataset all
```

脚本会备份原文件为 `.bak`（如 `question.json.bak`），原地写回。

### 1. 导入题集（import-cases）
可重复执行，根据题集中的id字段更新题目
```bash
# dvdrental 导入（upsert，dataset+question 唯一键）
python eval/eval.py import-cases --file eval/dvdrental/question.json --name dvdrental --db business --status verified

# tpcds_sf1 导入
python eval/eval.py import-cases --file eval/tpcds_sf1/tpcds_eval_sf1_final.json --name tpcds_sf1 --db tpcds_sf1 --status verified
```

导入后数据写入 PostgreSQL `telemetry.eval_datasets` + `telemetry.eval_cases` 表。

### 2. 重放（replay）

启动后端：
```bash
# 停掉旧进程 → 重启
cd backend; .venv\Scripts\python.exe -m codegenx
```

逐题调 chat API 产生 trace，每题独立会话。**默认重放所有 `status='verified'` 题**。

```bash
# 重放 dvdrental 全部 46 题
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --dataset dvdrental

# 重放指定 case id（调试用）
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --ids 35,37

# 同时重放 dvdrental + tpcds_sf1
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --dataset dvdrental,tpcds_sf1

# 单题测试（加 --limit 限制题数）
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --dataset dvdrental --limit 3
```

| 参数 | 说明 |
|------|------|
| `--account / --password` | 评测专用账号（**不能有跨会话 warm memory**） |
| `--app-id` | 项目 ID |
| `--dataset` | 按 dataset 名过滤，如 `dvdrental` 或 `dvdrental,tpcds_sf1`（可选） |
| `--ids` | 只重放指定 case id（可选，不填则按 dataset 或全部 verified） |
| `--limit` | 最多重放多少题（默认 500，覆盖完整题集） |
| `--all` | 含 draft 题目（默认只重放 verified） |
| `--interval` | 题间间隔秒（避免 API 限流） |
| `--timeout` | 单题 SSE 读取超时秒 |

### 3. 评分（score）

读库评分：取 trace 内全部成功 SQL 连业务库重执行，与 gold 结果集对比。结果写入telemetry.trace_scores表

```bash
# 评全部
python eval/eval.py score

# 只评某时刻之后（避免重复评分）
python eval/eval.py score --since 2026-11-05T10:00:00

# 评指定 trace
python eval/eval.py score --trace-id eval-35-43c02c12
```

| 指标 | 含义 |
|------|------|
| **EX** | 执行准确率（1=agent SQL 结果与 gold 一致，0=不一致） |
| **VES** | 效率分（gold 耗时 / agent 耗时，≤1） |
| **Valid SQL Rate** | 至少一条 SQL 成功执行的比例 |

EX 判定分两级：

1. **严格匹配**：gold 列 ⊆ agent 列（多余列允许），按列签名逐行比较；行序无关。
2. **宽松匹配**（严格失败后启用，归类为 `ok_relaxed`）：只要求 gold 的每个值能在 agent 行中找到体现，容忍
   - 列的拆分/合并（`first_name`+`last_name` ↔ `"Mike Hillyer"`）与额外标识列（多输出 `staff_id`）
   - 数值末位差异（`ROUND(x,2)`、浮点精度，容差 0.01）
   - 日期粒度差异（`2005-05-01` ↔ `2005-05`）、时间戳微秒与 NULL 表示
   - 题目约定"结果超过 20 条只输出前 20 条"的 top-N 截断（agent 20 行对应 gold 中的 20 行）

失败归类：`no_sql` / `sql_error` / `db_unknown` / `gold_error` / `result_mismatch`；通过归类：`ok` / `ok_relaxed`。

### 4. 报告（report）

聚合 trace_scores + 逐题链路复现。**默认输出 `eval/report/report_YYYYMMDD_HHMMSS.md`**。

```bash
# 默认输出（自动时间戳，放 eval/report/）
python eval/eval.py report

# 聚合某时刻之后的 trace，一般score评分做了选取，这里也要选取相同时间范围的
python eval/eval.py report --since 2026-11-05

# 显式指定输出路径
python eval/eval.py report --out eval/report/my_test.md
```

报告内容分四块：
1. **总览**：评测范围 / 题数 / EX / Valid SQL Rate / VES
2. **题目概览**：数据集 / 难度分布 / 结果粒度 / ops Top 10
3. **报告概览**：难度分布（题数/通过/EX）、重点标签（cte、window_function 等数仓特征标签）、题目结果清单（case/难度/标签/结果/失败归因）
4. **逐题评估**：每道题完整执行链路复现（Thought → Tool → SQL → 最终回复）+ 召回评估（gold 表 vs agent 召回表）；LLM 输出统一用代码围栏包裹，避免与报告正文混排

## 数据库表

| 表 | 说明 |
|---|---|
| `telemetry.eval_datasets` | 题集注册（name 唯一） |
| `telemetry.eval_cases` | 评测题目（question / gold_sql / db_name / difficulty / ops / grain / multi_hop / status） |
| `telemetry.traces` | 评测 trace 主表（每次 replay 一条） |
| `telemetry.trace_spans` | trace span 树（turn → llm / tool / retrieval） |
| `telemetry.trace_scores` | 评分结果（trace_id × name=EX/VES/valid_sql） |
| `telemetry.tool_semantics` | 工具调用语义（SQL 全文 / 返回行数） |

## 典型评测命令组合

```bash
# 完整评测 dvdrental
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --dataset dvdrental
python eval/eval.py score
python eval/eval.py report

# 重打标签 → 同步 → 全量重评（题集有更新时）
python eval/retag_cases.py --dataset all
python eval/eval.py import-cases --file eval/dvdrental/question.json --name dvdrental --db business --status verified
python eval/eval.py import-cases --file eval/tpcds_sf1/tpcds_eval_sf1_final.json --name tpcds_sf1 --db tpcds_sf1 --status verified
python eval/eval.py replay --account boluo --password boluo123 --app-id app_0001 --dataset dvdrental,tpcds_sf1
python eval/eval.py score
python eval/eval.py report
```
