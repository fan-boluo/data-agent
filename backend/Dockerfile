# 后端多阶段构建：builder 装依赖 → runtime 跑服务
# 构建上下文要求在 backend/ 目录下执行：docker build -t codegenx-backend .

# ── Stage 1: Builder ──────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# uv 安装器（0.8.5）
COPY --from=ghcr.io/astral-sh/uv:0.8.5 /uv /usr/local/bin/uv

# 先复制依赖声明 → 利用 Docker 层缓存
COPY pyproject.toml uv.lock ./

# 同步依赖到 .venv（包含项目包安装）
RUN uv sync --frozen --no-dev

# ── Stage 2: Runtime ──────────────────────────────────────
FROM python:3.12-slim AS runtime

# 环境变量：uvicorn 不产 pyc + 关闭输出缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装时区数据 + 清理（让 apt layer 体积最小）
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && cp /usr/share/zoneinfo/${TZ} /etc/localtime \
 && echo ${TZ} > /etc/timezone \
 && apt-get purge -y --auto-remove tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 复制虚拟环境（已含所有依赖 + 项目代码）
COPY --from=builder /app/.venv ./.venv
ENV PATH="/app/.venv/bin:$PATH"

# 复制配置文件 + 运行时数据目录占位
COPY config.json ./config.json
COPY .env.example ./.env.example

# 数据持久化目录（看板 JSON、会话快照、聊天记录），compose 挂载到 /data
RUN mkdir -p /data /app/logs

# 非 root 用户运行（安全加固）
RUN groupadd -r codegenx && useradd -r -g codegenx -d /app codegenx \
 && chown -R codegenx:codegenx /app /data
USER codegenx

EXPOSE 8456

# 健康检查：5 秒一次，连续 3 次失败判定 unhealthy
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8456/api/health', timeout=3)" || exit 1

# 启动入口（uvicorn 在 .venv/bin 中）
CMD ["python", "-m", "codegenx"]
