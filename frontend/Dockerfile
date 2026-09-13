# syntax=docker/dockerfile:1
# ============================================================================
# 前端镜像：node 构建 → nginx 托管
#
# 构建要点：
# 1. **必须用 .dockerignore 排除 node_modules**。否则 `COPY . .` 会把宿主机
#    （macOS arm64）的依赖覆盖掉容器里刚装好的 Linux 版本，报
#    "Cannot find module @rollup/rollup-linux-arm64-gnu" 之类的原生模块错误。
#    这是 pnpm + Docker 组合最经典的坑。
# 2. 用 npm 全局装 pnpm 而不是 corepack：corepack 在 Node 25+ 已被移除，
#    依赖它意味着某天升级基础镜像后构建会突然失败。
# ============================================================================

# ---------------------------------------------------------------------------
# 构建阶段
# ---------------------------------------------------------------------------
FROM node:24-alpine AS builder

# 钉住版本，保证与 pnpm-lock.yaml 的格式一致
RUN npm install -g pnpm@11.24.0

WORKDIR /app

# 依赖层：只拷贝清单文件，改业务代码时不必重装依赖
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir=/pnpm/store

COPY . .

# 接口基地址在构建期被写进产物（Vite 会把 import.meta.env.VITE_* 内联）。
# 默认是相对路径 /api/v1：nginx 把 /api/ 反代到后端容器，
# 因此本地与线上行为一致，前端代码里没有任何环境判断。
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN pnpm run build

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM nginx:1.29-alpine AS runtime

COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# alpine 自带 busybox 的 wget，不需要额外装东西
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
