# SystemApp 运行时架构设计

## 核心思想

进程内所有长生命周期对象收敛为一个全局容器 **SystemApp**。

三层模型判别法（新代码决定东西放哪时逐条过）：

```
┌─────────────────────────────────────────────────────────┐
│ SystemApp（进程级，全局一份，启动装配 / 关闭回收）            │
│  组件 = 无状态服务 | 注册表 | 客户端池 | 后台任务            │
├─────────────────────────────────────────────────────────┤
│ RuntimeSessionState（会话级，SessionPool 管理，随会话生灭）  │
│  chat_messages / 锁 / turn / 活跃任务字典                    │
├─────────────────────────────────────────────────────────┤
│ ActivateTurn（请求级，单轮请求状态容器）                      │
└─────────────────────────────────────────────────────────┘
```

| 判别问题 | 结论 |
|---|---|
| 它随会话生灭吗？ | 否 → 候选组件 |
| 属性里除了 ids（session/app/user/路径）还有什么？ | 只有 ids → **伪会话对象**，服务化（方法保留，实例消失，ids 进签名） |
| 有每轮被覆写的可变字段吗？ | 有 → 会话/请求级状态；无 → 组件 |
| 持有连接 / 线程 / 后台任务吗？ | 有 → 必须是组件（由容器广播生命周期） |

---

## 组件清单（16 个）

按注册顺序排列——顺序即依赖，勿随意调整。

| # | 组件 | 实现类 | 说明 |
|---|---|---|---|
| 1 | llm_facade | LLMFacade | LLM 多 provider 统一门面（DashScope / DeepSeek / vLLM / 自定义），预热默认模型 |
| 2 | tool_registry | ToolRegistry | 启动即扫描 tools/ 目录，注册所有继承 BaseTool 的工具类；`child_view()` 产出过滤子视图（零重扫） |
| 3 | tool_executor | ToolExecutor | 统一工具执行入口 + `safe_path_guard` hook |
| 4 | skill_manager | SkillManager | 可插拔技能系统（SKILL.md 提示词模板装载） |
| 5 | agent_registry | AgentRegistry | 智能体规格注册表（AgentSpec：id / role / name / cls / tools 白名单） |
| 6 | session_persistence | SessionPersistence | 会话快照落盘 + 按 (user, app, session) 锁表 |
| 7 | task_board | TaskBoardService | 任务看板（按 request 维度 JSON 落盘，task_id 从 1 计数） |
| 8 | memory_manager | MemoryFacade | 三级记忆管理（Hot / Warm / Session），qdrant 预热 + ensure_warm_collection |
| 9 | session_summary | SessionSummaryService | 会话摘要提取（阈值状态 SummaryState 随会话生灭） |
| 10 | compaction | CompactionService | 上下文压缩（LLM 韧性链 + 会话级熔断器） |
| 11 | chat_message_store | ChatMessageStore | 聊天记录 MySQL DAO |
| 12 | message_bus | MessageBus | 消息总线（SSE 事件发布，不承载 agent 间消息） |
| 13 | trace_maintenance | MonitorMaintenanceService | 全链路追踪 + 周期维护任务 |
| 14 | memory_scheduler | MemoryScheduler | 记忆调度器（崩溃恢复 + 轮询循环） |
| 15 | session_pool | SessionPool | 内存会话池（LRU + idle 回收 + swap-out + 优雅停止） |
| 16 | chat_service | ChatService | 请求入口（薄委托层，投递给 session worker） |

### 生命周期钩子

组件按需实现三个钩子（不写即空）：

| 钩子 | 方向 | 广播入口 | 顺序 | 典型用途 |
|---|---|---|---|---|
| `on_init` | 启动（同步） | init_app() | 注册序 | 轻量装配（如 skill 加载） |
| `async_before_start` | 启动（异步，**主钩子**） | start_app() | 注册序 | 连接建立 / 后台任务启动 / 预热 |
| `async_before_stop` | 关闭（异步，**主钩子**） | stop_app() | **注册逆序** | 后台任务停止 / 连接释放 |

关闭异常隔离：单组件 `async_before_stop` 抛错只记日志，其余组件照常收尾——保证全部组件都有机会执行清理。

### 失败语义

| 语义 | 含义 | 适用 |
|---|---|---|
| **降级** | 失败仅记日志，不阻断启动 | 预热类、调度类（memory 预热、周期维护） |
| **fail fast** | 校验失败抛错，拒绝带病启动 | AgentRegistry 校验（工具/skill 名真实存在、name 不重复） |

### 组件注册约定

- **构造函数首参必须是 `system_app`**：`SystemApp.register(cls)` 以 `component(system_app)` 构造，因此 `__init__(self, system_app=None, ...)` 是约定
- **`name` 用 ComponentType 枚举**：容器内按 `.value` 字符串为 key
- **失败即 raise**：未注册即访问 → ValueError；name 重复 → RuntimeError；宁可显式失败也不静默兜底

---

## 两段式启动 & 逆序关闭

```
main.py lifespan（FastAPI 入口）
│
├─ startup：init_app() + start_app()
│   │
│   ├─ init_app()          同步装配
│   │   ├─ 创建全局容器 SystemApp
│   │   ├─ initialize_components()
│   │   │    ├─ 注册 16 组件（含 AgentRegistry 规格注册）
│   │   │    ├─ initialize_agent_instances()  ← leader / planner / data_explore / csv_explore
│   │   │    └─ hook_manager.load_and_freeze()  ← 最后：@on 监听器已全部收集
│   │   └─ _app.on_init()   广播同步钩子
│   │
│   └─ await start_app()    异步启动
│       └─ _app.async_before_start()  广播异步钩子（LLM 预热 / SessionPool cleanup loop / 记忆调度器启动）
│
└─ shutdown：await stop_app()
    ├─ _app.async_before_stop()  注册逆序广播
    └─ 基础设施兜底（suppress 全部异常，close 幂等）：
        redis_client.aclose() → shutdown_qdrant_client() → shutdown_mysql_engine() → shutdown_postgres_engine()
```

**幂等守卫**：`_components_ready` 防重复注册；`_started` 防重复启动。

---

## 全局访问

项目任意处通过 `get_app()` 拿容器，再用类型化访问器取组件：

```
get_app().agents.get("leader")          # AgentRegistry → AgentSpec
get_app().tool_registry.child_view(["execute_postgresql", "search_tables"])
get_app().session_pool.get_or_create(request)
get_app().memory.warm_extract(query)
```

访问器内部是**惰性 import + 按 ComponentType 查表**，业务代码零感知组件化。

辅助函数：

| 函数 | 用途 |
|---|---|
| `reset_app()` | 仅测试用：卸载容器（**严禁**调 hook_manager.reset()，会永久清空监听器） |
| `app_started()` | 诊断：容器是否已完成启动 |

---

## 导入约束（防循环）

- `system_app.py` 模块级只 import 框架层（`component.py`），**所有组件类都延迟 import**（initialize_components / 访问器内部）
- 组件模块允许在模块级 import 框架再导出（`from codegenx.ai_service.system_app import BaseComponent`）
- 组件模块间横向取其他组件：经 `get_app().xxx` 运行期访问，**不**模块级 import

---

## 红线速查

| 红线 | 原因 |
|---|---|
| 组件 `__init__` 首参必须是 `system_app` | `register` 以 `component(system_app)` 构造 |
| hook 监听器必须是裸函数（无 self） | `@on` 注册裸函数，分发器直接 `callback(ctx)` |
| 新监听器模块必须在 import 链上 | 收集发生在模块加载，冻结后不可再注册 |
| initialize_components 注册顺序不可乱 | 顺序即依赖（如 chat_service 依赖 message_bus / session_pool） |
| 业务代码只经 `get_app().xxx` 访问组件 | 防止第二套模块级单例 |
| 组件不得持有会话/智能体可变状态 | 多租户多智能体下串话比崩溃更难排查 |
| 关闭用 `suppress` 兜底基础设施 | 进程级幂等，单连接 close 失败不阻断其余 |

---

## 文件地图

```
backend/src/codegenx/ai_service/
├── component.py            # 框架层（无业务 import）：LifeCycle / ComponentType / BaseComponent / SystemApp(注册表+广播)
├── system_app.py           # 组合根：SystemApp(具体容器 + 16 个类型化访问器)
│                           #         initialize_components / init_app / start_app / stop_app / get_app
├── agent/
│   ├── agent_instances.py  # initialize_agent_instances：leader / planner / data_explore / csv_explore 规格注册
│   ├── tool_handler.py     # ToolRegistry（启动扫描 + child_view）
│   ├── tool_executor.py    # ToolExecutor
│   └── agent_registry.py   # AgentRegistry + AgentSpec
├── session/session_pool.py # SessionPool
├── chat_message/
│   ├── persist.py          # SessionPersistence
│   └── store.py            # ChatMessageStore
├── task/task_manager.py    # TaskBoardService
├── memory/memory_manager.py # MemoryFacade
├── llm/facade.py           # LLMFacade
├── bus/queue.py            # MessageBus
├── services/chat_service.py # ChatService（请求入口）
├── compact/                # SessionSummaryService + CompactionService
├── trace/                  # MonitorMaintenanceService + recorder（直调，不挂 hook）
├── schedule/memory.py      # MemoryScheduler
└── hook/                   # HookManager（监听器随各功能模块 @on 注册，末尾 load_and_freeze）
```
