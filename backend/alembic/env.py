"""Alembic 迁移环境（异步驱动）。

连接串来自应用配置而非 alembic.ini，理由有两个：
1. 密码只有 ``.env`` 一处来源，不会被提交进版本库；
2. 迁移与运行时连的一定是同一个库，不会出现「迁移跑到 A 库、服务连的是 B 库」。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db import models  # noqa: F401  必须导入，autogenerate 才能发现全部表
from app.db.base import Base

config = context.config

# 从应用配置注入连接串。
# 注意 % 必须转义成 %%：configparser 会把 %(...)s 当成插值语法，
# 密码里一旦含 % 就会抛 InterpolationSyntaxError。
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _common_context_kwargs() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        # 默认只比较表/列的新增删除，这两项打开后连「字段类型改了」「默认值改了」
        # 也能被 autogenerate 发现，否则要等到线上报错才知道
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库（``alembic upgrade head --sql``）。"""
    context.configure(
        url=settings.database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_context_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        include_schemas=False,
        **_common_context_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # 迁移是一次性任务，用完即走，不需要连接池
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
