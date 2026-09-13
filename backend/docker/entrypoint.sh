#!/bin/sh
# ============================================================================
# 后端容器入口：等数据库 → 跑迁移 → 启动服务
#
# 把迁移放在入口脚本里而不是让运维手动执行，是为了消灭
# "服务起来了但表结构是旧的" 这类问题——它往往要到某个请求报错才被发现。
# ============================================================================
set -eu

echo "[entrypoint] 等待数据库 ${POSTGRES_HOST:-localhost}:${POSTGRES_PORT:-5432} …"

# 用 asyncpg 做一次**真实查询**而不是 TCP 连通性检查：
# 数据库进程在接受 TCP 连接后仍可能处于恢复中、拒绝查询。
# 只看端口通不通会得出"已就绪"的错误结论，随后迁移立刻失败。
if ! python - <<'PY'
import asyncio
import os
import sys

import asyncpg

TIMEOUT_SECONDS = 60


async def wait_for_database() -> bool:
    deadline = asyncio.get_event_loop().time() + TIMEOUT_SECONDS
    attempt = 0

    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        try:
            connection = await asyncpg.connect(
                host=os.environ.get("POSTGRES_HOST", "localhost"),
                port=int(os.environ.get("POSTGRES_PORT", "5432")),
                user=os.environ.get("POSTGRES_USER", "docparser"),
                password=os.environ.get("POSTGRES_PASSWORD", ""),
                database=os.environ.get("POSTGRES_DB", "smart_doc_parser"),
                timeout=3,
            )
            await connection.execute("SELECT 1")
            await connection.close()
            print(f"[entrypoint] 数据库已就绪（第 {attempt} 次尝试）")
            return True
        except Exception as exc:
            if attempt % 5 == 1:
                print(f"[entrypoint] 尚未就绪：{type(exc).__name__}: {exc}")
            await asyncio.sleep(1)

    return False


if not asyncio.run(wait_for_database()):
    print(f"[entrypoint] 等待数据库超过 {TIMEOUT_SECONDS} 秒，放弃启动", file=sys.stderr)
    sys.exit(1)
PY
then
    exit 1
fi

echo "[entrypoint] 应用数据库迁移 …"
alembic upgrade head

echo "[entrypoint] 启动服务（workers=${BACKEND_WORKERS:-1}）…"

# exec 让 uvicorn 成为 1 号进程，从而能正确接收 SIGTERM 并优雅关闭
exec uvicorn app.main:app \
    --host "${BACKEND_HOST:-0.0.0.0}" \
    --port "${BACKEND_PORT:-8000}" \
    --workers "${BACKEND_WORKERS:-1}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
