# Hook 设计

> 版本：v3.0 · 2026-11-02
> 相关代码：`backend/src/codegenx/ai_service/hook/`

## 核心原则

Hook 是执行流上的**拦截 / 扩展点**，核心价值是把**横切关注点**（监控、日志、安全、记忆、注入、策略）从核心执行循环里抽离出来——让主流程保持显式、可推理，同时允许这些能力按需插入。

**判断"要不要放 hook"的试金石**：删掉这个 hook，主流程是否还能正确运行。

---

## 10 类事件

事件命名统一 snake_case。每类事件在 `events.py` 的 `EVENT_DEFINITIONS` 中声明，包含：默认分发模式、是否可短路、异常策略、载荷约定。

| 事件 | 触发时机 | 默认模式 | 可短路 | 异常策略 | 当前监听器 |
|---|---|---|---|---|---|
| `on_user_input` | 用户输入进入系统 | SERIAL | ✅ | BLOCKED | inputput_safety_check |
| `on_session_start` | 会话首次创建 | SERIAL | ❌ | RAISE | （预留） |
| `on_turn_start` | 单轮请求开始 | SERIAL | ❌ | SWALLOW | （预留） |
| `before_llm_invoke` | LLM 调用前 | SERIAL | ✅ | BLOCKED | （预留） |
| `after_llm_invoke` | LLM 返回后 | PARALLEL | ❌ | SWALLOW | （预留） |
| `before_tool_call` | 工具调用前 | SERIAL | ✅ | BLOCKED | safe_path_guard / file_tool_param_guard / sql_trace_start |
| `after_tool_call` | 工具返回后 | PARALLEL | ❌ | SWALLOW | persist_tool_log / tool_semantics_recorder / sql_trace_end |
| `on_turn_end` | 单轮结束（finally，含异常路径） | SERIAL | ❌ | SWALLOW | memory_turn_signal |
| `on_session_end` | 会话关闭 | SERIAL | ❌ | RAISE | memory_session_end |
| `internal/on_error` | 请求执行异常（内部事件） | SERIAL | ❌ | SWALLOW | （预留） |

> `internal/*` 前缀保留给框架内部事件，不参与业务 trace，不对外承诺稳定性。

### 异常策略含义

| 策略 | 含义 |
|---|---|
| **RAISE** | 异常上抛（lifecycle 类：初始化失败应可见地失败） |
| **BLOCKED** | 记入 errors 并按 blocked 短路（hook 失败默认阻断下游动作） |
| **SWALLOW** | 仅记录日志与 errors，主流程继续（finally 语义 / 只读上报类） |

---

## 当前已注册监听器清单

| 监听器 | 事件 | priority | 位置 | 功能 |
|---|---|---|---|---|
| `inputput_safety_check` | on_user_input | 10 | `guardrail/prompt_safety_input_guardrail.py` | 用户输入安全检查，可 blocked |
| `safe_path_guard` | before_tool_call | 30 | `agent/tool_executor.py` | 工具调用路径安全守卫，可 blocked |
| `file_tool_param_guard` | before_tool_call | 20 | `tools/base.py` | 文件工具参数守卫，可 blocked |
| `sql_trace_start` | before_tool_call | 100 | `hook/analysis_trace.py` | SQL 工具调用追踪起点（condition=_is_execute_sql） |
| `persist_tool_log` | after_tool_call | 10 | `chat_message/persist.py` | 工具执行日志落盘 |
| `tool_semantics_recorder` | after_tool_call | 50 | `hook/tool_semantics.py` | 工具语义记录 |
| `sql_trace_end` | after_tool_call | 100 | `hook/analysis_trace.py` | SQL 工具调用追踪终点 |
| `memory_turn_signal` | on_turn_end | 20 | `memory/trigger.py` | 记忆系统轮次信号触发 |
| `memory_session_end` | on_session_end | 20 | `memory/memory_manager.py` | 记忆系统会话末触发 |

priority 数值**小者先执行**（同事件内）。`condition` 可按谓词跳过本次分发。

---

## 三种分发模式

| 模式 | 特点 | 适用场景 |
|---|---|---|
| **SERIAL（顺序）** | 按冻结顺序逐个 await；支持 blocked/inject 短路协议 | 安全检查（要串行）、lifecycle 钩子 |
| **PARALLEL（并行）** | `asyncio.gather(return_exceptions=True)`；异常隔离，不可短路 | 监控上报、日志落盘（互不依赖） |
| **WATERFALL（洋葱）** | hook 层层包裹 `call_next`；不调用即短路；调用链自动记录 span | 上下文组装（暂未启用） |

### 短路协议

SERIAL 模式的监听器可返回 `HookDecision`：

| 返回值 | 含义 |
|---|---|
| `None` | 放行，继续后续 hook |
| `HookDecision(action="blocked", message="...")` | 短路：阻断下游动作，主流程按 blocked 处理 |
| `HookDecision(action="inject", message="...")` | 不阻断，但注入一条提示消息到上下文 |

---

## 核心架构

```
┌─────────────────────────────────────────────────────────┐
│ 触发方（AutoPlanManager / BaseReActAgent / ToolExecutor）  │
│   await hook_manager.emit("before_tool_call", ctx)        │
└────────────────────────┬────────────────────────────────┘
                         │ emit / emit_parallel
┌────────────────────────▼────────────────────────────────┐
│ hook/ 框架层（只含机制，不含业务监听器）                     │
│   HookManager：注册表 + 拓扑排序 + 三种分发                  │
└────────────────────────▲────────────────────────────────┘
                         │ import 时自注册（@on 装饰器）
                         │
         ┌───────────────┼───────────────┐
         │               │               │
    guardrail/       tools/base.py   memory/
    inputput_        file_tool_     memory_turn_signal
    safety_check     param_guard    memory_session_end
         │               │               │
         └───────────────┼───────────────┘
                         │
              各功能模块（监听器写在自己归属的模块里，不新建 builtin 目录）
```

---

## @on 装饰器

监听器通过 `@on` 装饰器自注册，模块 import 时收集到 pending 列表，**不立即生效**——避免模块重复导入导致重复注册，也允许启动前统一校验。

### 装饰器参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `event` | 必填 | 事件名，必须是 EVENT_DEFINITIONS 中已声明的事件 |
| `name` | 函数名 | hook 唯一标识，`depends_on` 与查重均按此名 |
| `priority` | 100 | 数值小者先执行（同事件内，无依赖关系的 hook 之间按此决胜） |
| `depends_on` | [] | 声明"我必须在哪些 hook 之后执行" |
| `mode` | 事件默认 | 可覆盖 EVENT_DEFINITIONS 的默认分发模式 |
| `condition` | None | 谓词 `(ctx) -> bool`，False 则本次分发跳过 |
| `once` | False | True 则首次触发后自动注销 |

---

## 启动注册流程

```
main.py lifespan
  └─ SystemApp.initialize_components()
      ├─ 各业务组件 import（触发所有 @on 装饰器收集到 hook_manager._pending）
      ├──────└─ hook_manager.load_and_freeze()
      │         │
      │         ├─ ① 校验：
      │         │     - 事件名合法
      │         │     - hook name 无重复
      │         │     - depends_on 指向存在的 hook
      │         │     - 依赖图无环（Kahn 拓扑排序成功）
      │         │
      │         ├─ ② 每个事件按 拓扑序(priority 决胜) 冻结监听器列表
      │         │
      │         └─ ③ 冻结后调用 @on 直接抛错（防止运行期热改注册表）
      │
      └─ 启动各组件 broadcast_start → Runtime 就绪
```

---

## HookContext（统一上下文）

每类事件创建时生成一个 `HookContext`，贯穿整个分发链：

| 字段 | 说明 |
|---|---|
| `event` | 当前事件名 |
| `session` | RuntimeSessionState（会话态：request / user_id / app_id / context_manager…） |
| `turn` | ActivateTurn（轮次态：step_counter / active_step_id…） |
| `data` | 事件专属载荷 dict（如 tool_call / messages / response） |
| `action` | 分发器回填：continue / blocked / inject |
| `message` | blocked / inject 附带消息 |
| `errors` | 分发过程中收集的异常字符串列表 |

---

## 触发点位置

Hook 事件在以下位置被触发：

| 事件 | 触发位置 |
|---|---|
| `on_user_input` | ChatService 请求入口（用户消息进入系统时） |
| `on_session_start` | SessionPool 创建新会话时 |
| `on_turn_start` | AutoPlanManager.execute_request 入口 |
| `before_llm_invoke` | BaseReActAgent 调用 LLM 前 |
| `after_llm_invoke` | BaseReActAgent 收到 LLM 响应后 |
| `before_tool_call` | ToolExecutor 执行工具前 |
| `after_tool_call` | ToolExecutor 工具执行返回后 |
| `on_turn_end` | AutoPlanManager.execute_request finally 块（**含异常路径**，保证触发） |
| `on_session_end` | SessionPool.close_session_unsafe 关闭会话时 |
| `internal/on_error` | AutoPlanManager / BaseReActAgent 异常捕获分支 |

---

## 目录结构

```
ai_service/hook/
├── __init__.py          # 导出 on、hook_manager、HookContext、HookDecision、事件常量
├── events.py            # HookEvent 常量 + EventDefinition + EVENT_DEFINITIONS 表
├── core.py              # HookManager + @on 装饰器 + load_and_freeze
├── context.py           # HookContext / HookDecision / Span
├── analysis_trace.py    # SQL 追踪监听器（sql_trace_start / sql_trace_end）
└── tool_semantics.py    # 工具语义记录监听器（tool_semantics_recorder）
```

监听器**不集中放在 hook/ 目录**——各自归属功能模块：
- 安全守卫 → `guardrail/` / `tools/base.py`
- 日志落盘 → `chat_message/persist.py`
- 记忆触发 → `memory/trigger.py` / `memory/memory_manager.py`
