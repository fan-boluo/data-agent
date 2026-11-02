# AI 数据分析平台

AI 驱动数据应用快速交付平台，支持自然语言生成代码、数据分析、可视化报告，面向数据分析师。
![img_1.png](img_1.png)

## 技术架构

uv单体架构
```
┌──────────────────────────────────────────────────────┐
│                     Frontend (Vue 3)                  │
│         Ant Design Vue + Monaco Editor + ECharts      │
│                    Port: 5173 (dev)                   │
└──────────────────────┬───────────────────────────────┘
                       │ HTTP/SSE
┌──────────────────────▼───────────────────────────────┐
│                  API Gateway (FastAPI)               │
│         动态路由 · JWT 认证 · 限流 · IP 黑名单            │
│                    Port: 8456                        │
└─────────────────────────────────────────────────┘
                       │ 
┌──────────────────────▼──────────────────────────┐
│ User        App      AI             shared      │
│                                                 │
│ Infrastructure MySQL · Redis FastAPI  Prometheus│
└─────────────────────────────────────────────────┘
                                  
```

| 层级 | 技术选型 |
|---|---|
| 前端 | Vue 3 + TypeScript + Vite + Pinia + Ant Design Vue 4 |
| 编辑器 | Monaco Editor (`@guolao/vue-monaco-editor`) |
| 后端框架 | FastAPI (Python 3.12) |
| 数据库 | PostgreSQL 8.0 (SQLAlchemy + asyncpg 异步) |
| 缓存 | Redis 7 |
| 向量存储 | Qdrant |
| 监控 | Prometheus + OpenTelemetry |
| 容器化 | Docker Compose / Kubernetes |

## 基础设施

| 组件 | 用途 |
|---|---|
| **Redis** | 限流（固定窗口 INCR+EXPIRE）、记忆缓存 |
| **MySQL** | 用户类数据、记忆真源 |
| **PostgreSQL** | 用户项目库、公共库、语义层、监控存储（主业务数据库） |
| **Qdrant** | Warm 层记忆向量检索、语义表卡向量检索 |

## 模块说明（uv 单体架构，后续扩展可以拆分微服务）

```
backend/src/codegenx/
├── main.py               # 入口：组装全部 router 到同一个 app（统一 /api 前缀）
├── gateway/              # 请求入口层（路由在 main.py 最前面 include_router）
├── user_service/         # 用户模块（注册/登录/登出/用户信息）
├── app_service/          # 项目管理模块（代码文件、脚本运行、数据库）
├── ai_service/           # AI 核心（Agent Runtime / Hook / Memory / Tool / Trace）
├── shared/               # 共享（配置、异常、工具函数）
├── db/                   # 基础设施客户端（MySQL / PostgreSQL / Redis / Qdrant）
└── tests/                # 测试
```

| 模块 | 路由前缀 | 职责 |
|---|---|---|
| **gateway** | `/api/*` | JWT 认证、限流（固定窗口 5/s）、IP 黑名单、SSE 事件总线 |
| **user_service** | `/api/user/*` | 注册 / 登录 / 登出 / 用户信息（密码 MD5+盐加密） |
| **app_service** | `/api/app/*` | 项目 CRUD、代码文件树、在线编辑（Monaco Editor）、脚本运行、数据库创建删除 |
| **ai_service** | `/api/chat/*`、`/api/ai/*` | Agent 运行时（leader → 子智能体内联）、会话池、Hook 事件系统、三级记忆、全链路追踪 |
| **metrics** | `/metrics` | Prometheus 指标（无 `/api` 前缀，单独暴露） |

## 主要功能模块

### 项目管理
- 创建/删除/编辑应用项目
- 在线代码编辑器（语法高亮、文件树、多 Tab）
- Python 脚本在线运行，实时查看输出
- 代码文件上传、下载、重命名
- 项目数据库表结构浏览

### AI 对话式开发
- 自然语言描述需求，Agent 自动生成代码
- 多轮对话，支持流式输出
- 会话历史查看与继续
- 会话停止/中断控制
- 用户级频率限制

### 数据可视化 TODO
- ECharts 图表渲染
- 数据表浏览与查询
- 统计面板（Token 消耗、请求量、费用）

### 监控管理 (Admin)
- 会话监控面板（运行中/已完成/失败）
- 告警规则与告警记录
- Token 用量查询
- 历史数据清理

### 用户与权限
- 用户注册 / 登录（JWT）
- 管理员角色（`admin`）
- 普通用户（`user`）项目隔离
- IP 黑名单管理

## Agent 技术架构总览
基于事件触发的AgentRuntime
```
                        ┌─────────────────┐
                        │   MessageBus    │  ← 事件总线
                        └────────┬────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │           AgentRuntime               │
              │  ┌─────────────────────────────────┐ │
              │  │         Dispatch Loop           │ │
              │  └───────────┬─────────────────────┘ │
              │              │                        │
              │  ┌───────────▼──────────┐             │
              │  │     SessionPool      │ ← 会话池    │
              │  │  (max 1000, TTL 1h)  │             │
              │  └───────────┬──────────┘             │
              │              │                        │
              │  ┌───────────▼──────────┐             │
              │  │   Turn Loop (≤50)    │ ← 多轮对话  │
              │  │  ┌────────────────┐  │             │
              │  │  │ Context Builder│  │ ← 上下文    │
              │  │  │ + Compaction   │  │   组装+压缩  │
              │  │  └───────┬────────┘  │             │
              │  │          ▼           │             │
              │  │  ┌────────────────┐  │             │
              │  │  │   LLM Client   │  │ ← 模型    │
              │  │  │  + Recovery    │  │   容错重试   │
              │  │  └───────┬────────┘  │             │
              │  │          ▼           │             │
              │  │  ┌────────────────┐  │             │
              │  │  │ Tool Executor  │  │ ← 工具调用  │
              │  │  │ (14+ tools)    │  │             │
              │  │  └───────┬────────┘  │             │
              │  │          ▼           │             │
              │  │  (loop if tools)     │             │
              │  └──────────────────────┘             │
              └───────────────────────────────────────┘
```

### 核心组件

| 组件 | 说明                                                                 |
|---|--------------------------------------------------------------------|
| **AgentRuntime** | 中央调度器，管理消息总线、会话池、工具注册、钩子系统                                         |
| **SessionPool** | 内存会话池，最多 1000 个并发会话，空闲自动回收                                     |
| **Turn Loop** | 多轮对话循环，限制每轮最大迭代次数，每次迭代组装上下文 → LLM 推理 → 工具调用                        |
| **Context Builder** | 动态组装系统提示词 + 历史消息 + 工具结果，超长自动压缩                                     |
| **LLM Client** | 基于 OpenAI 兼容协议，支持 DashScope / DeepSeek / Zhipu / vLLM / 自定义        |
| **Tool System** | 20+ 工具：代码检查、CSV/MySQL 数据操作、文件读写、搜索（find/grep）、任务面板、子 Agent、Skill 加载 |
| **Hook System** | 生命周期钩子：会话启停、轮次启停、LLM 前后、工具前后、错误处理                                  |
| **Memory** | 三级记忆：Hot（全局 MEMORY.md）、Warm（关键词召回主题文件）、Session（当前会话）               |
| **Skill Runtime** | 可插拔技能系统：数据清洗、特征工程、建模、绘图、Python 分析、SQL 分析、报告生成                      |
| **Task Board** | 会话内持久化任务面板，Agent 可自行规划/追踪子任务                                       |
| **Monitor Pipeline** | 全链路追踪：会话录制 → Span 采集 → 指标计算 → 告警评估 → Prometheus 暴露                 |


### 环境要求

python虚拟环境所在位置: .venv\Scripts\pyhton.exe

- Python 3.12+
- Node.js 20+
- PostgreSQL 8.0
- Redis 7
- Qdrant

> 启动 PostgreSQL、Redis、Qdrant、Prometheus
并修改配置文件中的参数为实际参数 ：
 系统参数：backend/.env 
 智能体参数：backend/config.json
     选择默认使用的模型，并设置agents列表中的参数defaults=True,填写你需要的provider ，并在下面的providers列表中补充url及密钥


### 手动
#### 1. 后端启动

```bash
# 安装依赖
cd backend
uv sync --default-index https://mirrors.aliyun.com/pypi/simple/

# 配置环境变量 (.env config.json)

# 启动
python backend/src/codegenx/__main__.py

```

#### 2. 前端启动

```bash
cd frontend
pnpm install
pnpm dev
```

#### 3. 停止服务

Ctrl+C 或关闭终端即可。

### Docker 部署（推荐用于生产）

一键拉起全部服务（前端 + 后端 + PostgreSQL + Redis + Qdrant）：

```bash
# 1) 准备后端配置（必填项：数据库密码、JWT、LLM provider API Key）
cp backend/.env.example backend/.env
# 编辑 backend/.env 填入真实密码和 Key

# 2) 准备 compose 根目录变量
cp .env.example .env
# 编辑 .env 填入 POSTGRES_PASSWORD / REDIS_PASSWORD

# 3) 构建并启动（首次 build 较慢，后续 compose 会复用镜像层）
docker compose up -d --build

# 4) 查看运行状态
docker compose ps
docker compose logs -f backend        # 看后端日志
docker compose logs -f frontend       # 看前端 Nginx 日志

# 5) 访问
#    前端：http://localhost
#    API 健康检查：http://localhost/api/health
#    Prometheus 指标：http://localhost/metrics

# 6) 停止并清理（注意 -v 会清空数据卷，谨慎使用）
docker compose down
docker compose down -v                # 彻底清除数据（仅调试阶段用）

# 7) 重新构建某个服务（代码更新后）
docker compose up -d --build backend
docker compose up -d --build frontend
```

#### 容器说明

| 服务 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| **frontend** | `codegenx-frontend` (nginx:alpine) | 80（可改 FRONTEND_PORT） | 托管 Vite 构建产物，反向代理 `/api` → backend |
| **backend** | `codegenx-backend` (python:3.12-slim) | 内部 8456 | FastAPI + uvicorn，只在 compose 网络内暴露 |
| **postgres** | `pgvector/pgvector:pg16` | 内部 5432 | 主业务库 + 语义层 + pgvector 扩展 |
| **redis** | `redis:7-alpine` | 内部 6379 | 缓存 + 限流 |
| **qdrant** | `qdrant/qdrant` | 内部 6333 | 向量存储（记忆 + 语义检索） |

#### 数据持久化

compose 定义了 4 个 named volume，容器重建不会丢数据：

| Volume | 挂载路径 | 内容 |
|---|---|---|
| `pg_data` | `/var/lib/postgresql/data` | PostgreSQL 主库 |
| `redis_data` | `/data` | Redis AOF 持久化 |
| `qdrant_data` | `/qdrant/storage` | Qdrant 向量数据 |
| `codegenx_data` | 后端容器 `/data` | 看板 JSON / 会话快照 / chat_message |

> ⚠️ `docker compose down -v` 会删除全部 volume。如果只是重启服务，用 `docker compose down`（不加 -v）。

#### 目录结构

```
CodeGenX/
├── docker-compose.yml            # 一键编排（根目录）
├── .env.example                  # compose 根变量（POSTGRES_PASSWORD 等）
├── .dockerignore                 # 根目录构建忽略
│
├── backend/
│   ├── Dockerfile                # 后端多阶段构建（python:3.12-slim 运行）
│   ├── .dockerignore
│   ├── .env.example              # 后端运行时配置模板
│   └── src/ ...
│
└── frontend/
    ├── Dockerfile                # 前端多阶段构建（nginx:alpine 运行）
    ├── .dockerignore
    ├── docker/
    │   └── nginx.conf            # SPA 路由 + /api 反向代理 + SSE 长连接
    └── src/ ...
```

#### 生产部署检查清单

- [ ] 后端 `backend/.env` 中 `JWT_ALGORITHM=RS256`，配好私钥路径（不要用 HS256 对称密钥）
- [ ] `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `JWT_SECRET` 改掉默认值
- [ ] 后端 `config.json` 中的 LLM provider API Key 已填入
- [ ] 配置 HTTPS（Nginx 加 SSL 或前置 LB）
- [ ] 数据库初始化脚本（如果有 DDL 初始化）放到 `init/postgres/` 下，compose 会自动执行
- [ ] 日志持久化（Docker 日志默认在 `/var/lib/docker/containers/`，建议配日志驱动）

## 其它详细设计说明

docs/ 下：

- [Runtime与系统架构设计.md](./docs/Runtime与系统架构设计.md) — 系统总览、JWT、限流、Runtime 请求全链路、SystemApp 组件容器
- [多Agent设计.md](./docs/多Agent设计.md) — 智能体架构、agent-as-tool 内联执行、看板显式管理
- [数据分析工具技能设计.md](./docs/数据分析工具技能设计.md) — 工具注册机制、工具清单、Manager 设计、大表处理策略
- [Hook设计.md](./docs/Hook设计.md) — 事件清单、分发模式、启动注册、HookContext
- [Session管理.md](./docs/Session管理.md) — 会话生命周期、SessionPool LRU + cleanup、停止机制
- SystemApp架构设计.md
- 上下文工程设计.md
- LLM调用设计方案.md
- 记忆系统设计.md
- 语义层设计.md
- 用户项目权限与存储隔离方案.md