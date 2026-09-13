"""异步数据库引擎与会话。

两条使用路径，**不要混用**：

1. **请求内**：通过 ``Depends(get_db)`` 拿到会话，随请求结束自动关闭。
2. **后台任务内**：用 ``session_scope()`` 自己开一个会话。

后台任务绝不能复用请求的会话——请求一结束会话就关了，
再往里写会报 "This session is closed" 或悄悄泄漏连接池里的连接。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        # 连接空闲后被数据库/中间网络设备掐断时，取用前先探活，避免第一个请求报错
        pool_pre_ping=True,
        # 回收超过 30 分钟的连接，配合云数据库的连接超时设置
        pool_recycle=1800,
    )


#: 模块级引擎。create_async_engine 是惰性的——这里不会真的连数据库，
#: 因此本模块可以在没有数据库的环境下被导入（例如导出 OpenAPI schema 时）。
engine: AsyncEngine = _create_engine()

#: 会话工厂。expire_on_commit=False 让 commit 后仍能读取 ORM 对象的属性，
#: 否则在异步上下文里访问会触发隐式 IO 并抛 MissingGreenlet。
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        # 注意：这里**不**自动 commit。事务边界由 service 层显式控制，
        # 避免出现「读操作意外提交了别处的半成品写入」这类问题。


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """后台任务专用：自建会话并保证关闭。"""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database() -> None:
    """探活：执行一条最轻量的查询，供 /health/ready 使用。"""
    from sqlalchemy import text

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    """进程退出时释放连接池。"""
    await engine.dispose()
    logger.debug("database_engine_disposed")
