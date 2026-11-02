# Session 生命周期管理

## 概述

会话由三层状态共同决定：**前端 localStorage**、**后端内存池（SessionPool）**、**磁盘持久化（chat_message + 任务看板 JSONL）**。每层各有独立过期策略。

---

## 过期时间

| 层级 | 失效条件 | 时间 |
|---|---|---|
| JWT token | 登录凭证过期 | 24 小时 |
| 前端空闲登出 | 用户 30 分钟无操作 | 30 分钟 |
| 后端 SessionPool | 内存池 idle 驱逐 | idle_timeout_seconds（默认 1800s = 30min） |
| 后端 SessionPool swap | 卸载 chat_messages 节省内存 | swap_idle_seconds（默认 300s = 5min） |
| 磁盘 session 数据 | 手动清理 / 定期归档 | — |

---

## Session ID 生命周期

### 何时新建

| 条件 | 说明 |
|---|---|
| 用户退出→重新登录 | `doLogout` 清除所有 localStorage 键，下次打开为新 session |
| 用户点击「新会话」按钮 | 清除 localStorage、生成新 ID、清空聊天消息 |
| 打开页面时 session 已过期 | `checkSessionAlive` 返回 false → 清旧 localStorage → 新 ID |
| 首次打开项目 | localStorage 中无 session 记录 → `ensureChatSessionId` 生成新 ID |
| 空闲登出 | 清除 token 和 session 键，下次登录等同新用户 |
| 发消息时 session 被驱逐 | 后端 `get_or_create` 发现 pool 中无此 session → 自动重建，前端不感知 |

### 何时复用

| 条件 | 说明 |
|---|---|
| 页面打开，session 还在 pool 中 | `checkSessionAlive` 返回 true → 加载历史消息，继续聊天 |
| 正常聊天过程中 | `get_or_create` 命中 → `touch()` 更新 `last_activity_at` → 重置 idle 倒计时 |

---

## SessionPool 设计

### 核心特性

- **LRU 管理**：`OrderedDict` 维护会话有序性，超过 `max_sessions` 时驱逐最久未访问
- **自动清理**：后台 `_cleanup_loop()` 定时（默认 300s）扫描，执行三类清理动作
- **优雅停止**：`stop_request` 和 `stop_session` 带 `stop_signal` + grace 等待（默认 2s）
- **容器化装配**：`BaseComponent` 子类，生命周期由 SystemApp 广播接管（`async_before_start` / `async_before_stop`）

### 三类清理动作

| 类型 | 触发条件 | 动作 |
|---|---|---|
| **Idle 回收** | `idle_duration > idle_timeout_seconds` 且 **quiescent**（无在途任务） | 摘要提取 → 关闭 → 从池中移除 |
| **Swap-out** | `idle_duration > swap_idle_seconds` 且 quiescent | 摘要提取 → 卸载 `chat_messages`（置空），会话对象保留；下次请求由 runtime 按需从快照恢复 |
| **已关闭清理** | `session.closed = True` | 直接移除 |

> **关键防误杀**：`_is_quiescent()` 检查 `processing` 标志、`active_tasks` 是否全部 done、`worker_task` 是否 done。执行中的会话（有活跃任务）即使 idle 超时也不回收。

### 驱逐策略

```
新请求到达 → get_or_create()
  ├─ session_id 命中 → move_to_end + touch() → 返回
  └─ 未命中 → 池已满？
       ├─ 是 → 驱逐 LRU（OrderedDict 首个） → 创建新 session
       └─ 否 → 直接创建新 session
```

### 停止机制

**统一 `stop_signal` 检查点 + `TurnStoppedError` 异常处理**

两个入口，分离实现：

| 方法 | 作用域 | 行为 |
|---|---|---|
| `stop_request(session_id, request_id)` | 停止**单个请求** | 设置 `session.stop_signal` + grace 等待活跃任务自行走到检查点；待处理请求从队列丢弃；**保留会话** |
| `stop_session(session_id)` | 停止**整个会话** | 清空 pending 请求 → grace 等待活跃任务 → 关闭 worker + 所有活跃任务 → 标记 closed → 从池中移除 |

> `stop_signal` 是 session 级 `asyncio.Event`——当前 worker 顺序处理请求，同一时刻只有一个活跃请求，session 级信号等价于 per-request 信号。

**执行流中的停止检查点**：`runtime_schema.raise_if_stop_signal()` 在 AutoPlanManager 和 BaseReActAgent 的关键循环处调用，检测到信号立即抛 `TurnStoppedError`，上层捕获后发布 `REQUEST_STOPPED` 事件收尾。

### 关键防竞态

**会话复用不改写 `session.request`**：`request` 由 worker pop 时统一设置；入队时改写会致执行中的请求 `request_id/publish_context/db_name` 串号——看板写入他人 request 文件、SSE 事件发错订阅队列。

---

## RuntimeSessionState（运行时会话状态）

每个 session 对应一个 `RuntimeSessionState` 实例，持有：

| 属性 | 说明 |
|---|---|
| `session_id` | 前端生成的唯一标识 |
| `request` | 当前正在处理的请求对象 |
| `context_manager` | `SessionContext`：**chat_messages 会话历史真相源**、db_name、用户信息 |
| `activate_turn` | 当前轮次状态（step_counter / active_step_id / state） |
| `active_tasks` | 活跃的 asyncio.Task 字典（request_id → task） |
| `pending_requests` | 排队中的请求列表（worker 顺序消费） |
| `stop_signal` | `asyncio.Event`，停止信号 |
| `stop_reason` | 停止原因字符串 |
| `publish_context` | SSE 订阅上下文（request_id → 队列路由） |

---

## 状态恢复流程

```
打开项目页面
  │
  ├─ localStorage 中有 sessionId？
  │   ├─ 是 → GET /api/.../alive 检查存活
  │   │   ├─ alive=true → 加载历史消息 → 接着聊
  │   │   └─ alive=false → 清理 localStorage → 新 sessionId → 空白页
  │   └─ 否 → 新 sessionId → 空白页
```

Swap-out 恢复：session 命中 pool 但 `chat_messages` 为空 → runtime 从磁盘快照恢复历史消息后继续。

---

## 空闲自动登出

前端 `useIdleTimeout.ts` 在 `GlobalHeader.vue` 中启用：

- 监听全局 `mousemove` / `keydown` / `click` / `scroll` / `touchstart`
- 30 分钟无操作 → 清除 token + 所有 session localStorage → 跳转登录页

---

## 关键 API

| 端点 | 说明 |
|---|---|
| `GET /api/chat/sessions/{appId}/{sessionId}/alive` | 检查 session 在内存池是否存活 |
| `GET /api/chat/sessions/{appId}/{sessionId}/messages` | 从磁盘加载 session 历史消息 |
| `GET /api/chat/sessions/{appId}` | 列出 app 下最近 sessions |
| `POST /api/chat/sessions/{sessionId}/stop` | 停止会话（stop_session） |
| `POST /api/chat/requests/{requestId}/stop` | 停止单个请求（stop_request） |

---

## 相关文件

| 文件 | 职责 |
|---|---|
| `ai_service/session/session_pool.py` | SessionPool：LRU + cleanup + 停止机制 |
| `ai_service/agent/runtime_schema.py` | RuntimeSessionState + TurnStoppedError + stop_signal |
| `ai_service/context/session_context.py` | SessionContext：chat_messages 真相源 |
| `ai_service/services/chat_service.py` | ChatService：会话请求入口（薄委托层） |
