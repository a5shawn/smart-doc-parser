# syntax=docker/dockerfile:1
# ============================================================================
# 后端镜像
#
# 构建要点：
# 1. **builder 与 runtime 用同一个基础镜像** + UV_PYTHON_DOWNLOADS=0。
#    否则 uv 可能自行下载一个解释器，venv 里的路径指向 builder 阶段的目录，
#    复制到 runtime 后直接失效——表现为 "python: command not found" 或
#    "No module named app"，而构建阶段一切正常。
# 2. **依赖层与代码层分离**：先只拷贝 pyproject.toml + uv.lock 装依赖，
#    改业务代码时这一层能命中缓存，不必重装依赖。
# 3. **非 root 运行**：容器被攻破时，攻击者拿到的也不是 root。
# ============================================================================

# ---------------------------------------------------------------------------
# 构建阶段：安装依赖
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# 只取 uv 的二进制，而不是 FROM astral-sh/uv —— 这样对镜像的 tag 命名规则不敏感
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# 依赖层。--mount=type=bind 让这两个文件不进入镜像层，只用于本步骤
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# 运行阶段
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# 与 builder 保持一致，避免任何解释器路径上的差异
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    UV_PYTHON_DOWNLOADS=0

RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 业务代码与迁移脚本。alembic 目录必须在，容器启动时要跑 upgrade head。
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser alembic.ini pyproject.toml uv.lock ./
COPY --chown=appuser:appuser docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    # 上传目录：即使没挂数据卷也要能写，否则首次启动就会失败
    && mkdir -p /app/data/uploads \
    && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

# 健康检查用 python 的 urllib 而不是 curl：
# slim 镜像里没有 curl，为一个探针装它不值得（还多一个攻击面）。
# 这里刻意不用 $ 符号，避免 compose 解析时的插值问题。
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
