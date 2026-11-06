# LLM 熔断器测试

端到端验证「模型级熔断 → 快速失败 → 冷却半开 → 探测恢复」。
测试脚本只做客户端：起本地故障注入代理把后端 LLM 流量导向它，通过代理控制面在正常 / 503 之间切换，
再读 `/metrics` 的 `codegenx_llm_circuit_state` 判定熔断状态。

## 文件说明

| 文件 | 作用 |
|---|---|
| `llm_fault_proxy.py` | 本地 HTTP 反代（默认 :8899）：正常转发 DashScope，支持全量 / 按模型注入 503；控制面 `/__ctl/{break,heal,state}` |
| `serve_with_proxy.py` | 启动后端（:8457），启动时把 `providers.dashscope.api_base` 指向代理（不改 `config.json`） |
| `test_circuit_breaker.py` | 测试客户端：登录 → 对话 → 读 metrics → 逐模型推进恢复 → 生成报告 |
| `circuit_breaker_report.md` | 最近一次运行自动生成的报告 |

## 前置条件

- 依赖服务（MySQL / PostgreSQL / Redis / Qdrant）已启动；
- `backend/config.json` 默认智能体的 `model_name` 是**多模型逗号链**（每个模型一个独立熔断器），
  当前为 3 个：`qwen3.8-max-0902 → glm-5.3 → deepseek-v4.1-flash`；
- Key 有效（预热与恢复阶段是真实调用，会产生少量 token）；
- 每次重跑前重启 `serve_with_proxy.py`：熔断状态在进程内存里，上一轮残留会让阶段 [0] 就出现 `half_open/open`；
- 使用默认熔断参数：`breaker_failure_threshold=5`、`max_attempts=2`、`breaker_recovery_timeout_seconds=30`、
  `half_open_max_calls=2`、`half_open_success_rate=0.5`。

## 运行方式

三个终端，均在 `backend/` 下：

```powershell
.venv\Scripts\python.exe tests\llm_breaker\llm_fault_proxy.py      # 1. 故障代理 :8899
.venv\Scripts\python.exe tests\llm_breaker\serve_with_proxy.py     # 2. 后端 :8457
.venv\Scripts\python.exe tests\llm_breaker\test_circuit_breaker.py # 3. 执行测试
```

启动终端 2 后应看到预热快照 `[llm_breaker] startup 共 N 个熔断器: closed×N`。

## 测试流程

| 阶段 | 动作 | 预期结果 |
|---|---|---|
| `[0]` 预热 | 代理正常，发 1 次对话 | `OK`，`closed×N` |
| `[1]` 注入故障 | `POST /__ctl/break` 全量 503，连续请求直到全 OPEN | 请求失败，最终 `open×N` |
| `[2]` 快速失败 | 再发 1 次请求 | 失败但耗时低一个量级 |
| `[3]` 链首恢复 | 等 `--recovery-wait`（默认 45s）→ `heal`，最多探测 10 次 | `half_open` → `closed×1 / open×(N-1)` |
| `[4]` 全链恢复 | 逐个把「目标前面的非 open 模型」临时 break，把流量逼到目标，验证 `half_open → closed` | 链上每个模型至少一次 `closed` |

## 测试项与预期输出

参考输出（3 模型链）：

```
[0] 预热：验证代理链路可用
  预热（正常）               OK     13.53s  closed×3
[1] 注入上游 503（RETRYABLE → 累计熔断失败）
  故障请求 #1              FAIL    6.56s  closed×3
  故障请求 #2              FAIL    6.78s  open×3
[2] 全熔断期：验证快速失败
  全熔断（快速失败）            FAIL    1.59s  open×3
[3] 冷却 45s 后恢复上游，验证 half_open → closed
  恢复探测 #1              OK      2.75s  half_open×1 / open×2
  恢复探测 #2              OK      2.78s  closed×1 / open×2
  ...（#3~#10 维持 closed×1 / open×2）
[4] 全链恢复：链 = qwen3.8-max-0902 → glm-5.3 → deepseek-v4.1-flash
    已恢复：qwen3.8-max-0902
    → 目标 glm-5.3：临时故障化 qwen3.8-max-0902，把探测流量让给它
  推进 1-1               OK      5.73s  closed×1 / half_open×1 / open×1
  推进 1-2               OK      2.80s  closed×1 / open×2
    ✓ glm-5.3 已 half_open → closed
    → 目标 deepseek-v4.1-flash：临时故障化 glm-5.3，把探测流量让给它
  推进 2-1               OK      2.56s  closed×1 / half_open×1 / open×1
  推进 2-2               OK      2.52s  closed×1 / open×2
    ✓ deepseek-v4.1-flash 已 half_open → closed
[4] 最终验证恢复：qwen3.8-max-0902, glm-5.3, deepseek-v4.1-flash（3/3）
```

### [0] 预热

- **预期**：`OK`，`closed×3`。
- **原因**：熔断器冷启动即 `CLOSED`，`get_breaker` 创建时就把 state gauge 置 0，此时的 `acquire()` 恒放行。
- **关键**：若起始出现 `half_open/open`，是上一轮进程残留，重启服务后重跑。

### [1] 故障请求 #1 / #2

- **预期**：#1 `FAIL，closed×3`（约 6.5s）；#2 `FAIL，open×3`（同量级）。
- **原因**：单请求对每个模型最多 `max_attempts+1=3` 次尝试（首发 + 2 次退避重试），503 属于 `RETRYABLE`，每次都计熔断失败。
  #1 后每个模型累计 3 次 < 阈值 5，仍 `CLOSED`；#2 再累计 3 次 = 6 ≥ 5，逐个 `OPEN`。
  #2 不会更快：模型是「本轮先真实失败 3 次、累计到阈值」才打开的，冷却后的快速跳过只发生在后续请求。
- **关键**：只有 `RETRYABLE` 计入 CLOSED 的连续失败；`FATAL`（401/403/404）、`OVERFLOW` 不累计（设计文档 §4.1）。

### [2] 全熔断期

- **预期**：`FAIL`，约 1.6s，比故障期低一个量级。
- **原因**：`OPEN` 且冷却未满时 `acquire()` 直接返回 False，链上每个模型都被跳过，不发起任何上游调用，
  只剩链遍历与 SSE 收尾开销。

### [3] 链首恢复

- **预期**：#1 `half_open×1 / open×2`；#2 起稳定 `closed×1 / open×2`，并跑满 10 次探测。
- **原因**：`--recovery-wait` 45s > 冷却 30s，恢复后首个请求让链首 `OPEN → HALF_OPEN` 并拿到 1 个探测名额，
  探测成功只记 `half_total=1`；#2 第二次探测成功 → 探测数 2、成功率 100% ≥ 50% → `CLOSED`。
  此后请求永远命中已闭合的链首，后两个模型的 `acquire()` 不再被调用，状态停在 `OPEN`——这是设计中的链上短路。
- **关键**：脚本「全部 closed」的退出条件在该设计下不可达，所以会跑满 10 次；看 #2 出现 `closed×1` 即判定通过。

### [4] 全链恢复

- **预期**：每个目标 1~2 次请求，先 `half_open`、再 `closed`，最终 `N/N`。
- **原因**：
  1. 先临时 break 目标前面**所有非 open** 的模型：否则前序 `closed` 模型会截住 fallback 流量，目标永远收不到探测；
  2. 目标冷却期已过，`acquire()` 自动转 `HALF_OPEN` 并放行名额；第 1 次探测成功只记 1 次，显示 `half_open×1`；
  3. 第 2 次请求：被 break 的前序模型累计失败达到阈值重新 `OPEN`，目标第 2 次探测成功 → `CLOSED`；随后 `heal` 前序模型；
  4. 每轮首条 `推进 x-1` 偏慢（如 5.73s）是因为被 break 的前序模型此时仍 `CLOSED`，要等它 3 次重试耗尽才落到目标；
     到 `x-2` 它已 `OPEN`，耗时回到约 2.5s。
- **关键**：目标若本轮探测失败（如 FATAL 配额耗尽），按设计会重新 `OPEN` 并重置冷却，不会闭合；
  查 `logs/app.log` 中该模型的 `[llm_call]` 是否为 `outcome=error_fatal`。

## 关键说明

- **断言是「每个模型都曾 CLOSED」，不是「最终全部 CLOSED」**：fallback 每次从链首开始，链上只要还有
  `CLOSED` 模型就会短路后续模型，全链同时闭合在该设计下不可达。
- **半开探测名额**：上限 `half_open_max_calls=2`；任一次探测失败（含 FATAL/OVERFLOW）都回 `OPEN` 并以本次失败
  重新计时冷却；请求取消会归还名额。名额只出不还会让熔断器永久卡在 `HALF_OPEN`（见设计文档 §4.2「探测名额不变量」）。
- **观测**：`/metrics` 的 `codegenx_llm_circuit_state{circuit="dashscope:<model>"}`（0=closed / 1=half_open / 2=open）；
  `logs/app.log` 的 `[llm_breaker] OPEN/HALF_OPEN/CLOSED`、`[llm] ... 跳过（熔断打开）`、`[llm_call] ... outcome=error_*`。
- **SSE 判定**：全链失败由业务层包装成 SSE `Error` 事件，脚本按 `"event_type":"Error"` / `"state":"failed"` /
  `LLMNoModelAvailable` 判失败。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` / `--proxy-url` | `127.0.0.1:8457` / `127.0.0.1:8899` | 后端 / 代理控制面地址 |
| `--account` / `--password` | `boluo` / `boluo123` | 登录账号 |
| `--app-id` | `app_0001` | 目标应用 |
| `--question` | `只回复两个字：收到` | 测试问题（越短越快） |
| `--recovery-wait` | `45` | 冷却等待秒数，需 > `recovery_timeout`（30s） |
| `--chain` | 空（读 `config.json`） | 模型链，逗号分隔且按 fallback 顺序 |
| `--skip-full-chain` | 关闭 | 跳过阶段 [4] |

代理控制面（阶段 [4] 用）：

```powershell
curl -X POST http://127.0.0.1:8899/__ctl/break                       # 全量故障
curl -X POST http://127.0.0.1:8899/__ctl/heal                        # 全量恢复
curl -X POST "http://127.0.0.1:8899/__ctl/break?model=glm-5.3"       # 单模型故障
curl -X POST "http://127.0.0.1:8899/__ctl/heal?model=glm-5.3"
curl http://127.0.0.1:8899/__ctl/state
```

## 常见问题

- **上游 404**：`--proxy-url` 少了 `/compatible-mode/v1` 前缀；
- **连不上 8457**：终端 2 未起服务；
- **预热失败**：代理未启动，或 `config.json` 的模型名 / Key 有误；
- **[4] 推进不动**：`--recovery-wait` 太短（目标冷却未过，`acquire()` 不会转半开），或 `--chain` 顺序与 `config.json` 不一致；
- **[4] 某模型始终无法 CLOSED**：查该模型 `[llm_call]` 是否为 `outcome=error_fatal`（典型 403 `AllocationQuota.FreeTierOnly`
  免费额度耗尽）——模型本身不可用，每次冷却后的探测都会被拒并重新 OPEN，属预期行为。
