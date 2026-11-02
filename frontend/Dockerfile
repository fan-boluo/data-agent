# 前端多阶段构建：builder 装依赖 + 产出 dist → runtime Nginx 托管
# 构建上下文要求在 frontend/ 目录下执行：docker build -t codegenx-frontend .

# ── Stage 1: Builder ──────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# pnpm 安装 + 镜像源加速
RUN npm config set registry https://registry.npmmirror.com \
 && npm install -g pnpm@9

# 先复制 lock + package → 利用 Docker 层缓存
COPY pnpm-lock.yaml package.json ./

# 严格按 lock 安装（无 lock 则安装失败）
RUN pnpm install --frozen-lockfile

# 复制源码 + 构建（Vite 默认输出到 dist/）
COPY . .
RUN pnpm build

# ── Stage 2: Runtime ──────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# 时区
ENV TZ=Asia/Shanghai
RUN apk add --no-cache tzdata \
 && cp /usr/share/zoneinfo/${TZ} /etc/localtime \
 && echo ${TZ} > /etc/timezone \
 && apk del tzdata

# 自定义 Nginx 配置（SPA history 路由 + /api 反向代理）
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

# 构建产物复制到 Nginx 默认目录
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# Nginx 前台运行（容器默认）
CMD ["nginx", "-g", "daemon off;"]
