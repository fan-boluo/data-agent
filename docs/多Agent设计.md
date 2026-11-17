# 多智能体架构设计

> 版本：v2.0 · 2026-11-02
> 核心变化：从多跳接力路由 → **单跳直通 + agent-as-tool 内联执行**

---

## 1. 架构总览

### 设计原则

- **单跳直通**：调度循环只剩 `user → leader → user`，子智能体通过 `dispatch_agent` 工具**内联执行**（agent-as-tool），不再产生跨 agent 消息跳
- **看板显式管理**：任务看板由 LLM 经 `task_create / task_update / task_get / task_list` 工具**主动管理**，派发无隐式看板副作用
- **统一工具面**：leader 既能自己调工具，也能派发子智能体（`dispatch_agent` 是一个普通工具），还能派 planner 规划后逐步执行
- **执行上下文真相源**：`SessionContext.chat_messages` 是会话历史唯一真相源（供 LLM 读取 + 子智能体结果内联写入）

### 核心目录

```
backend/src/codegenx/ai_service/
├── agent/
│   ├── auto_plan_manager.py         # 请求执行入口（单跳直通 user→leader）
│   ├── runtime_schema.py            # RuntimeSessionState / TurnStoppedError / stop_signal
│   ├── agent_instances.py           # 组合根：initialize_agent_instances + _AGENT_CLASSES
│   ├── agent_registry.py            # AgentRegistry + AgentSpec（规格注册表）
│   ├── agent_schema.py              # AgentState / AgentEventType
│   ├── tools/
│   │   ├── tool_executor.py         # ToolExecutor（统一执行 + 熔断记账 + span）
│   │   ├── tool_guards.py           # BEFORE_TOOL_CALL 门禁（许可规则链 + HITL 审批）
│   │   ├── permission.py            # PermissionManager（存在性/配额/路径/熔断规则）
│   │   ├── tool_context.py          # 入参注入（仅 session_id）+ 按签名装配执行参数
│   │   ├── tool_circuit.py          # 工具级熔断准入/归还/记账
│   │   └── tool_handler.py          # ToolRegistry（启动扫描 + child_view 过滤）
│   ├── agents/
│   │   ├── agent.py                 # Agent 抽象基类 + AgentMessage
│   │   ├── base_react_agent.py      # BaseReActAgent（ReAct 循环 + tool_calls）
│   │   ├── leader_agent.py          # LeaderAgent（主控 + 编排入口）
│   │   ├── plan_agent.py            # PlannerAgent（计划智能体）
│   │   ├── data_explore_agent.py    # DateExploreAgent（数据探索）
│   │   ├── csv_explore_agent.py     # CsvExploreAgent（CSV 探索）
│   │   └── supervisor_board.py      # 看板协调（非 Agent，是 Leader 专用 Action）
│   └── db/                          # 数据库管理器（mysql_manager / postgres_manager / docker_exec）
├── tools/                           # 工具实现（继承 BaseTool + 约定式注册混合）
├── context/session_context.py       # chat_messages 会话历史真相源
├── task/task_manager.py             # TaskBoardService（按 request 落盘）
├── session/session_pool.py          # 会话池（LRU + cleanup + stop_signal）
└── hook/                            # Hook 事件系统
```

---

## 2. 智能体清单

| 智能体 | 定位 | 工具能力 | 是否注入会话历史 |
|---|---|---|---|
| **leader** | 主控：编排 + 直答 + 派发 | 全部工具（含 dispatch_agent + task_*） | ✅ 注入 `chat_messages[:-1]` |
| **planner** | 规划：拆解任务 + 登记看板 | 仅 task_* 工具（看板管理） | ❌ 任务自包含 |
| **data_explore** | 数据探索：查询 + 统计 | execute_sql / describe_table_stats / list_tables / describe_table / sample_rows | ❌ 任务自包含 |
| **csv_explore** | CSV 探索：文件读取 + 统计 | describe_csv / sample_csv_rows / describe_csv_stats / list_csv_files | ❌ 任务自包含 |
| **reporter** | 报告：图表规划 + 报告生成 | plot_chart / generate_report / run_python 等，skills=plot/report | ❌ 任务自包含 |

> 运行时可派发清单是动态的：被管理员全局停用、或模型链全部熔断打开的智能体不会出现在 leader 的可用清单中（见 §6 可用性拦截）。

### 注册机制

新增智能体只需三步：
1. 实现 Agent 子类（继承 `BaseReActAgent`）
2. 在类上声明 `profile`（PromptConfig 含 role / goal / constraints 等）
3. 在 `agent_instances.py` 的 `_get_agent_classes()` 中添加该类

启动时 `initialize_agent_instances()` 对每个类调用：
1. `cls.resolve_agent_config()` — 按 `cls.name` 从 config.json 匹配配置（模型链、max_steps、tools 白名单）
2. `cls.register()` — 构建 `AgentSpec`（id / role / name / desc / cls）注册到 `AgentRegistry`

config.json 中 `agents[].tools` 白名单决定该智能体能用哪些工具，由 `ToolRegistry.build_tools(allowlist, disabled)` 过滤产出 function-calling 目录：先剔除运行时不可用工具（平台总开关 / 项目授权，见《数据分析工具技能设计.md》资源管理节），再按白名单裁剪。

### 平台智能体管理（全局启停 + LLM 可用性）

- 表 `agent.agent`（init/params_schema.sql）只持久化**手动总开关** `is_enabled` 与备注，编码对应 `AgentSpec.name` / config.json `agents[].name`；只做全局层，无 app 级授权。
- **实际可用 = is_enabled ∧ 模型链存在未熔断模型**：LLM 可用性由 `resource_service/agents.py` 实时读取各模型 `CircuitBreaker.is_open()` 计算，不落库；模型链全部熔断打开即自动下线，冷却结束自动恢复，无需人工干预。
- 管理入口：平台资源页 Agent 管理 Tab（`GET/POST /params/agent/*`，仅管理员）；不可用（模型链全熔断）的智能体前端禁止开启，允许随时停用。

---

## 3. AgentMessage 协议

扁平一等字段，跨智能体消息统一格式：

| 字段 | 语义 |
|---|---|
| `sender` / `recipient` | 每跳必设。leader 派发时 recipient 设为目标智能体名 |
| `task` | 派发跳 = 任务文本；终答跳承载答复内容（与 result 协同） |
| `result` | 终答跳 = 面向用户的答复文本 |
| `request_id` / `session_id` | 贯穿始终，看板闭环握手 |
| `success` | 执行结果状态，终答跳必设 |

---

## 4. 执行型智能体（BaseReActAgent）

### ReAct 循环

`think → LLM → tool_calls → observation`，循环边界：observation 返回终答跳 或 达到 `max_steps`。

- **think**：组装上下文（SystemPrompt + chat_messages 历史 + LLM 上次输出）→ 调 LLM
- **act/tool_calls**：LLM 输出带 `tool_calls` → ToolExecutor 执行 → 结果作为 observation 进对话
- **终答**：LLM 输出纯文本（无 tool_calls）→ 构造 `AgentMessage(recipient="user")` 返回

### 会话历史注入

- **leader**：注入 `chat_messages[:-1]`（末条内容由本轮输入承载）
- **执行型 agent（data_explore / csv_explore / planner）**：**不注入**（任务自包含，只看 task 文本）

### 任务自包含原则

派发的 task 必须完全自包含——执行智能体看不到对话历史与其它任务，执行所需全部信息（文件名、数值、统计口径）必须直接写进 task。**跨数据源对比 / 汇总 / 结论生成是 leader 的职责**，禁止写进派发 task。

---

## 5. LeaderAgent 编排流程

### 单跳直通链路

```
用户请求
  │
  ▼
AutoPlanManager.execute_request()
  包装 AgentMessage(sender="user", recipient="leader")
  │
  ▼
LeaderAgent.run(msg, session_state)
  │
  ├─ think → LLM
  │
  ├─ 三种决策分支：
  │    │
  │    ├─ ① 直答（LLM 无 tool_calls，输出纯文本）
  │    │    → 构造终答跳 AgentMessage(recipient="user", result=文本) 返回
  │    │
  │    ├─ ② 自己用工具（LLM 输出普通工具的 tool_calls）
  │    │    → ToolExecutor 执行 → 结果进对话 → 继续 think 或终答
  │    │
  │    ├─ ③ 派发子智能体（LLM 输出 dispatch_agent 的 tool_calls）
  │    │    → dispatch_agent 工具内联执行目标智能体的完整 run()
  │    │    → 子智能体结果作为 tool result 回填对话
  │    │    → leader 继续 think（看到结果后决策下一轮：再派发 / 汇总 / 终答）
  │    │
  │    └─ ④ 派 planner（LLM 输出 dispatch_agent(agent="planner") 的 tool_calls）
  │         → planner 内联执行 → 产出任务分解 + 通过 task_* 工具登记看板
  │         → leader 看到计划后，亲自执行或逐步派子智能体
  │
  └─ 返回终答跳 → AutoPlanManager 收尾
       ├─ assistant 消息落 chat_message
       ├─ 会话历史快照
       ├─ hook: TURN_END
       └─ SSE REQUEST_COMPLETED 事件
```

### 多子任务并行

同一回复中多次调用 `dispatch_agent` = 并行执行，由 `ToolExecutor` 统一调度（工具内不做并行）：

- `dispatch_tool_calls` 把所有 tool_calls 交给 `asyncio.gather` 并发执行，`dispatch_agent` 额外受 `agent_slots`（`multi_agents.max_parallel`）限流；
- 普通工具受 `tool_slots`（`tools.max_concurrency`）限流；`dispatch_agent` 只占 `agent_slots`、不占 `tool_slots`，避免同轮多个派发吃光工具席位导致子 agent 内部工具饥饿（嵌套死锁）；
- 子 agent 内部工具走同一调度器，因此是"并行套并行"，嵌套深度恒为 1（子 agent 不能派子 agent）；
- 结果按原 tool_calls 顺序回填为 tool 消息，leader 在下一次 think 时一次看到全部子任务结果（协议要求 assistant.tool_calls 与 tool 消息成批配对，故批级可见）；
- `config.tools.execution_mode="sequential"` 可整体切换为串行执行，便于排障。

### 计划模式

1. **派 planner**：leader 调用 `dispatch_agent(agent="planner", task=用户原始请求)`
2. **planner 内联执行**：LLM 先规划 → 通过 `task_create` 创建看板任务（每个任务含 subject / description / depends_on DAG）
3. **leader 看到计划结果**（tool result 含看板文本 + 每个 task 详情）
4. **逐步执行**：leader 按顺序派子智能体或自己执行，完成后 `task_update(task_id=N, status="completed")` 解锁下游
5. **planner 无再派**：planner 只负责拆解和登记看板，不负责执行

### 防循环约束

- 已派发过的任务不重复派发：执行结果已在对话中（tool result），基于已有结果继续决策或汇总作答即可
- 回传结果出现矛盾时：最多派发一次口径明确的核实任务，核实结果回填后必须直接汇总作答
- 看板已有计划任务时禁止再派 planner

---

## 6. dispatch_agent 工具（agent-as-tool 核心）

`dispatch_agent` 是一个普通工具，让 leader 能把执行权内联交给子智能体：

**工具参数**：
- `agent`：目标智能体名（如 "data_explore" / "csv_explore" / "planner"）
- `task`：任务文本（必须自包含）

**执行流程**：
1. `dispatch_agent` 工具收到调用
2. **可用性拦截**：目标智能体被全局停用或模型链全部熔断打开时，直接返回 `ToolResult(success=False, error_class="agent_unavailable")`，leader 据此改派或自行处理
3. 内部按 agent 名从 `AgentRegistry` 拿到 `AgentSpec` → `spec.cls(context=SubContext(...))` 实例化（每调用新建实例，不缓存）
4. 包装 `AgentMessage(sender="leader", recipient=目标, task=task)`
5. `await 子智能体.run(msg, session_state)` 内联执行完整 ReAct 循环
6. 子智能体返回终答跳（或执行失败的回传跳）
7. 工具把子智能体结果作为 `ToolResult` 返回，leader 在下次 LLM 调用时看到

leader 的可用智能体清单（`agents_list_desc`）每轮实时构建，与被拦截口径一致：停用 / 全链熔断的子智能体不出现在清单中，LLM 不会看到不可派发的目标。

### 子智能体结果处理

- **成功**：`ToolResult(success=True, data="执行结果文本")` — leader 看到后继续决策
- **失败**：`ToolResult(success=False, message="执行失败：{reason}", error_class="subagent_failed")` — leader 看到后可重试 / 换路 / 汇总时承认失败

### 子智能体生命周期与通知

当前为**单向通信**（agent-as-tool 同步返回），子 agent 不主动向 leader 发消息：

| 通道 | 时机 | 消费方 |
|---|---|---|
| tool result | 子 agent `run()` 返回 | leader 下一次 LLM 调用 |
| `TOOL_EXECUTION_START` / `END` 事件（`tool_name="dispatch_agent"`） | 派发开始 / 结束 | 前端（SSE 实时展示） |
| `activate_turn.active_sub_agents` | 派发开始登记、结束回填 `finished_at` / `success` / `message` | 运行态观测（看板 / 排障） |

- `active_sub_agents` 的 key 为 `agent 名 + task_id/任务前缀`，支持同轮同 agent 多次派发的区分；
- 该注册表只做观测，不参与调度决策；turn 结束随 `ActivateTurn` 一起丢弃；
- 双向消息（mailbox / send-receive 队列）暂不引入：当前场景子任务都有终点、leader 需基于结果决策，agent-as-tool 足够；出现长时后台任务 / 跨请求任务时再升级。

---

## 7. 任务看板（TaskBoardService）

### 存储设计

- 按 **request 维度**存储：`.data/{user_id}/{app_id}/session/{session_id}/.tasks/{request_id}.json`
- 一个请求 = 一个 JSON 文件 = 全部任务
- `task_id` 按 request 从 1 计数（文件内 `max(id)+1`），`(request_id, task_id)` 复合键天然唯一

### TaskRecord 字段

| 字段 | 说明 |
|---|---|
| `id` | task_id（request 内从 1 起） |
| `subject` | 任务标题 |
| `description` | 任务详细描述 |
| `status` | pending / in_progress / completed / deleted |
| `executor_agent` | 计划时由 planner 指定执行智能体（可为空，leader 自行决定） |
| `blocked_by` | 依赖的前置任务 id 列表 |
| `blocks` | 本任务解锁的下游任务 id 列表 |
| `step_id` | 关联的执行步骤 id（可空） |

### 看板工具（LLM 显式操作）

| 工具 | 参数 | 功能 |
|---|---|---|
| `task_create` | subject, description, depends_on? | 创建任务（自动维护 blocked_by / blocks DAG） |
| `task_update` | task_id, status | 更新状态（completed 时自动从下游 blocked_by 移除并解锁） |
| `task_get` | task_id | 获取单任务详情 |
| `task_list` | — | 获取当前 request 看板全部任务（含 ready 标记和 blocked_by 信息） |

### 看板在 prompt 中的注入

- leader 的 `prompt_manager` 有 `taskbord_prompt="{{board_desc}}"` 模板位
- `_prepare_prompt_params` 自动调用 `TaskBoardService.get_board()` 获取文本板渲染注入

---

## 8. 上下文（Context）与运行时状态

### 上下文类型层级

```
CommonContext（基类：session_id / app_id / user_id / db_name / system_prompt / chat_messages）
├── SessionContext   ← leader 用：会话历史唯一真相源 + 摘要水位 + 快照/恢复
└── SubContext       ← 子 agent 用：一次性工作区，随 run 生灭，无水位/快照，额外带 agent_name
```

- `RuntimeSessionState.context` 类型为 `CommonContext`，运行时为 `SessionContext`——会话真相源不属于某一个 agent；
- `agent_name` 不在基类：leader 的归属在 `RuntimeSessionState.agent_name`，子 agent 的归属在 `SubContext.agent_name`；
- 子 agent 的 `SubContext` 由 `dispatch_agent` 创建并按 `cls(context=sub_context)` 注入，压缩只作用自己的工作区，不触达会话真相源。

### SessionContext（会话历史真相源）

`SessionContext.chat_messages` 是整个 AI Service 的**会话历史唯一真相源**：

- **写入点**：AutoPlanManager（用户消息 / assistant 终答）、BaseReActAgent（LLM tool_calls + tool result）
- **读取点**：leader 每轮 think 时从 SessionContext 取历史
- **快照持久化**：turn 结束后 `save_turn_chat_message_snapshot` 写磁盘（Swap-out 后恢复的依据）

### RuntimeSessionState 的职责边界

`RuntimeSessionState` 是 **session 级任务调度容器**，只包含任务调度 + 两个业务对象：

| 成员 | 定位 |
|---|---|
| `request_lock` / `pending_requests` / `active_tasks` / `worker_task` / `stop_signal` | 请求串行、任务生命周期、停止信号 |
| `context` | 会话上下文（`CommonContext`，运行时为 `SessionContext`） |
| `activate_turn` | 当前 turn 的请求级状态机 |

`ActivateTurn` 是**请求级单例，不按 agent 拆分**：

- `round_counter`（turn_id 落库与 SSE 轮次）、`state` / `started_at` / `finished_at`、`tool_call_counts`（工具配额）都是"一次用户请求"的语义，跨 agent 共享才是正确口径——一次请求的工具预算不该被子 agent 放大；
- 子 agent 需要独立的是上下文（`SubContext`）、trace（派发 span 下挂子 span）与结果（tool result），都不在 turn 上；
- 唯一的 agent 维度扩展是 `active_sub_agents`（观测注册表，见 §6）。

---

## 9. 看板文本格式（get_board 渲染）

```
## Task Board

- [ ] 1: 拉取销售总表数据  (blocked by: none)
- [>] 2: 计算月度销售趋势  (blocked by: 1)   ← in_progress
- [ ] 3: 对比各区域增速   (blocked by: 1)
- [x] 4: 生成可视化图表   (blocked by: 2,3) ← completed
```

状态标记：`[ ]` pending / `[>]` in_progress / `[x]` completed / `[-]` deleted。ready 标记 = 所有 blocked_by 任务均已 completed。

---

## 10. 停止机制

### 统一 stop_signal + TurnStoppedError

- `RuntimeSessionState.stop_signal`（`asyncio.Event`）是 session 级停止信号
- `raise_if_stop_signal(session_state)` 是执行流中的**检查点**，检测到信号立即抛 `TurnStoppedError`
- 检查点位置：
  - AutoPlanManager.execute_request 入口
  - BaseReActAgent ReAct 循环的每轮 think 前
  - dispatch_agent 内联执行前（子智能体 run 内也会在检查点抛出）

### stop_request vs stop_session

| 操作 | 作用域 | 行为 |
|---|---|---|
| `stop_request` | 单个请求 | 设置 stop_signal → grace 等待活跃任务自行走到检查点停止 → **保留会话** |
| `stop_session` | 整个会话 | 设置 stop_signal → grace 等待 → 关闭 worker + 所有活跃任务 → 从池中移除 |

详见 [SessionState管理.md](./SessionState管理.md)。

---

## 11. 关键变更记录

| 版本 | 变化 | 原因 |
|---|---|---|
| v1.0（已退役） | 多跳接力路由（Supervisor → Dispatch → 执行 Agent → 回传 → 审查 → 终答） | 原型验证 |
| v2.0 | 单跳直通 + agent-as-tool：用户→leader→用户，子智能体内联为 tool result | 多跳路由引入太多复杂性（批信封 / 并行路由 / 审查框架 / orchestration_state），agent-as-tool 更简洁、更符合主流设计（Anthropic Task / OpenAI Agents SDK as_tool） |
| v2.1（当前） | 智能体全局管理：agent.agent 手动启停 + 模型链熔断可用性实时判定，leader 清单过滤 + dispatch 拦截 | 平台侧需要紧急下线/恢复某个智能体的能力，且模型故障时自动摘派，避免反复派发注定失败的子任务 |
