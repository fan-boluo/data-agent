# Runtime 与系统架构设计

> session_pool 详细设计已移至 [SessionState管理.md](./SessionState管理.md)

---

## 1. 系统总览

整体分层：**API 网关 → 微服务层（User / App / AI）→ 基础设施**。uv 单体架构，服务间通过 Nacos 注册发现。

```
┌──────────────────────────────────────────────────────┐
│                     Frontend (Vue 3)                  │
│         Ant Design Vue + Monaco Editor + ECharts      │
│                    Port: 5173 (dev)                   │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP/SSE
┌──────────────────────▼───────────────────────────────┐
│                  API Gateway (FastAPI)                │
│         动态路由 · JWT 认证 · 限流 · IP 黑名单           │
│                    Port: 8456                         │
└───────────────────────────────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────────┐
│         AI Service (FastAPI, core)                     │
│                                                        │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐  ┌────────┐ │
│  │ Runtime │  │ Hook     │  │ Session   │  │ Trace  │ │
│  │ Agent   │  │ System   │  │ Pool      │  │ Monitor│ │
│  └─────────┘  └──────────┘  └───────────┘  └────────┘ │
│                                                        │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐  ┌────────┐ │
│  │ Context │  │ Compact  │  │ Memory    │  │ Skill  │ │
│  │ Builder │  │ 压缩     │  │ 三级      │  │ 技能   │ │
│  └─────────┘  └──────────┘  └───────────┘  └────────┘ │
│                                                        │
│  基础设施：MySQL · Redis · Qdrant · Postgres · Prometheus│
└───────────────────────────────────────────────────────┘
```

---

## 2. JWT 认证

### 当前实现

- 登录 → user-service 校验 → 网关签发 JWT（RS256，非对称加密） → 前端 localStorage 存储
- 后续请求头 `Authorization: Bearer <token>` → 网关 / 服务直接解析 JWT 鉴权
- Token 过期：硬性 **24h** 自动过期，前端设置 **30 分钟无操作退出**（`useIdleTimeout.ts`）减少过期重登
- **密钥改造已落地**：原 HS256 对称加密 → 现 RS256 非对称（私钥 auth-service 持有，公钥 gateway 持有）；payload 加入 `jti`（JWT ID，用于精准撤销）+ `iat`（签发时间）+ `aud`（目标服务限定）

### 退出机制（已确认）

用户主动退出：网关删除 refresh_token → 前端清 token + session localStorage → 下次登录等同新用户。

### 代码路径

- 网关中间件：`backend/api-gateway/middleware/jwt_auth.py`

---

## 3. 限流

### 实现方案

**固定窗口限流**（`api-gateway/services/rate_limit_service.py`）：
- 用户级限流，每秒 **limit=5**
- Redis 实现：`INCR + EXPIRE` 两行代码原子操作，零额外开销

### 为什么选固定窗口

AI 调用本身有秒级延迟，请求不是瞬时密集到达，不存在窗口边界的"双倍流量"问题。滑动窗口虽然理论上更平滑，但对 AI 场景是过度设计。

---

## 4. IP 黑名单

- FastAPI 拦截中间件，数据持久存储在 Redis Set
- 操作：`SISMEMBER / SADD / SREM / SMEMBERS / SCARD`

---

## 5. Runtime 架构（核心请求处理流程）

Runtime 是 AI Service 的心脏，**请求怎么被接收、处理、发送**的全链路。

### 5.1 核心组件

```
┌─────────────────────────────────────────────────────────┐
│ SystemApp（进程级组合根，启动装配全部组件）                  │
│                                                           │
│  SessionPool ─── 会话池（LRU + idle回收 + stop_signal）     │
│  ChatService ─── 请求入口（薄委托层）                        │
│  AutoPlanManager ── 请求执行入口（单跳直通：user → leader）  │
│  HookManager ─── 事件系统（9类事件 + 3种分发模式）            │
│  ToolRegistry ─── 工具注册表（启动扫描 + child_view 过滤）    │
│  AgentRegistry ── 智能体注册表（AgentSpec + spec.cls 实例化）│
│  LLMFacade ────── 模型统一门面（多 provider 适配）            │
│  MessageBus ───── 消息总线（SSE 事件下发）                    │
│  Trace ────────── 全链路追踪（recorder + api + store）        │
│  MemoryManager ─── 三级记忆管理                              │
└─────────────────────────────────────────────────────────┘
```

### 5.2 请求接收 → 处理 → 发送（全链路）

```
前端 POST /api/chat/generate
  │
  ├─ ① ChatService 接收请求
  │     ├─ 解析 request（message / session_id / metadata）
  │     ├─ SessionPool.get_or_create(request) → RuntimeSessionState
  │     │    └─ 命中 → touch 更新活跃时间；未命中 → 新建（含 SessionContext）
  │     └─ 投递给 session 的 worker（顺序消费 pending_requests）
  │
  ├─ ② Worker 执行 AutoPlanManager.execute_request(session_state)
  │     ├─ 包装 AgentMessage(sender="user", recipient="leader", task=用户文本)
  │     ├─ LeaderAgent.run(msg, session_state)  ← 单跳直通
  │     │    ├─ ReAct 循环：think → LLM → tool_calls → observation
  │     │    ├─ 子智能体经 dispatch_agent 工具内联执行（agent-as-tool）
  │     │    ├─ 终答跳 AgentMessage(recipient="user", result=答复文本)
  │     │    └─ 返回终答消息
  │     │
  │     ├─ 流式下发：leader ReAct 过程中的 LLM chunk 实时经 MessageBus 推送 SSE
  │     │
  │     ├─ 终答收尾：
  │     │    ├─ assistant 消息落 chat_message 表
  │     │    ├─ 会话历史快照 save_turn_chat_message_snapshot
  │     │    ├─ 触发 hook: TURN_END（finally，含异常路径）
  │     │    ├─ MessageBus.publish_runtime_event(REQUEST_COMPLETED)
  │     │    └─ trace turn_end 记录
  │     │
  │     └─ 异常分支：
  │          ├─ TurnStoppedError → REQUEST_STOPPED + state=STOPPED
  │          ├─ CancelledError → 同上（worker cancel 场景）
  │          └─ Exception → FAILED + Error 事件
  │
  └─ ③ SSE 消费端收到事件 → 前端渲染
        ├─ 过程事件：REQUEST_STARTED / TOOL_CALL / AGENT_MESSAGE / chunk 流
        └─ 终止事件：REQUEST_COMPLETED / REQUEST_STOPPED / Error
```


### 5.3 MessageBus 职责

MessageBus **仅用于用户请求 inbound 与 SSE 事件下发**：
- 内部不承载 agent 间消息（已随多跳路由退役）
- 核心接口：`publish_runtime_event(publish_context, event_type, ...)`
- 事件类型：`REQUEST_STARTED / REQUEST_COMPLETED / REQUEST_STOPPED / Error` + 过程事件

### 5.5 Hook 系统（事件拦截）

请求执行流的关键节点上挂 Hook：

```
execute_request 内部：
  hook: SESSION_START → TURN_START → (BEFORE_LLM_INVOKE → AFTER_LLM_INVOKE → (BEFORE_TOOL_CALL → AFTER_TOOL_CALL)*) → TURN_END → SESSION_END
```

HookManager 是 SystemApp 进程级组件，启动时校验依赖图后冻结注册表。详见 [Hook设计.md](./Hook设计.md)。

### 5.6 Trace 追踪

trace 模块直调记录（不寄生 hook 通道）：
- `trace_turn_start / trace_turn_end / trace_turn_error` 在 AutoPlanManager 的边界处调用
- recorder 记录完整执行轨迹，存 Postgres
- trace/api 提供查询服务

---

## 6. SystemApp 组件容器（组合根）

### 三层组件

```
┌─────────────────────────────────────────────────────────┐
│ SystemApp（进程级，全局一份，启动装配/关闭回收）              │
│  组件 = 无状态服务 | 注册表 | 客户端池 | 后台任务            │
├─────────────────────────────────────────────────────────┤
│ RuntimeSessionState（会话级，SessionPool 管理，随会话生灭）   │
├─────────────────────────────────────────────────────────┤
│ ActivateTurn（请求级，单轮请求的状态容器）                    │
└─────────────────────────────────────────────────────────┘
```

### 装配顺序（initialize_components）

```python
def initialize_components():
    # 1. 基础设施组件（底层依赖）
    session_pool = app.register(SessionPool)
    message_bus = app.register(MessageBus)

    # 2. 核心服务组件
    app.register(ToolRegistry)      # 启动即扫描 tools/ 目录
    app.register(ToolExecutor)
    app.register(SkillManager)
    app.register(LLMFacade)
    app.register(TaskBoardService)
    app.register(ChatService)
    app.register(AutoPlanManager)

    # 3. 智能体规格注册（依赖 ToolRegistry 已就绪）
    initialize_agent_instances(app)  # Leader / Planner / DataExplore / CsvExplore

    # 4. Hook 装载与冻结（所有监听器已随 import 链注册完毕）
    hook_manager.load_and_freeze()

    # 5. 启动广播
    app.broadcast_start()  # 触发各组件 async_before_start
```

### 生命周期广播

- **start**：SystemApp 启动时 → 对所有注册组件调用 `async_before_start()`
- **stop**：进程退出时 → 逆序调用 `async_before_stop()`（确保依赖组件先停）

---

## 7. 关键目录

```
backend/src/codegenx/ai_service/
├── system_app.py           # 组合根：组件装配 + 类型化访问器
├── component.py            # 框架：BaseComponent + LifeCycle + 注册表
├── session/
│   └── session_pool.py     # SessionPool（LRU + cleanup + 停止）
├── agent/
│   ├── auto_plan_manager.py    # 请求执行入口（单跳直通 user→leader）
│   ├── runtime_schema.py       # RuntimeSessionState + TurnStoppedError
│   ├── agents/                 # Agent 实现
│   ├── tool_handler.py         # ToolRegistry（启动扫描 + child_view）
│   ├── tool_executor.py        # ToolExecutor（统一执行）
│   ├── agent_instances.py      # 智能体组合根（initialize_agent_instances）
│   └── agent_registry.py       # AgentRegistry + AgentSpec
├── hook/                        # Hook 事件系统
├── tools/                       # 工具实现（BaseTool 继承 + 约定式注册混合）
├── context/                     # 上下文组装（SessionContext 为真相源）
├── compact/                     # 上下文压缩
├── memory/                      # 三级记忆
├── trace/                       # 全链路追踪
├── bus/                         # MessageBus（SSE 事件）
├── services/
│   └── chat_service.py         # ChatService（请求入口，薄委托层）
└── schema/                      # 请求/响应 schema
```

---

## 8. 启动流程

```
python -m codegenx
  │
  ├─ 读取 config.json（智能体配置：模型链、max_steps、tools 白名单）
  ├─ 读取 .env（项目配置：DB、Redis、端口）
  │
  ├─ SystemApp.initialize_components()
  │    ├─ 注册 SessionPool → 启动后台 cleanup loop
  │    ├─ 注册 ToolRegistry → 扫描 tools/ 目录 → 加载 BaseTool 子类
  │    ├─ initialize_agent_instances() → Leader/Planner/DataExplore/CsvExplore
  │    ├─ hook_manager.load_and_freeze() → 校验依赖图 + 拓扑排序 + 冻结
  │    └─ broadcast_start() → 触发各组件 async_before_start
  │
  └─ 启动 FastAPI + SSE 端点监听请求
```
