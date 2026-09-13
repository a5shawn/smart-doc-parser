"""pytest 共享夹具。

测试跑在**真实的 PostgreSQL** 上，而不是 SQLite：
本项目大量使用 JSONB、原生枚举、``gen_random_uuid()`` 这些 SQLite 没有的特性，
换库测试等于测了个不存在的系统。

集成测试执行前会先对测试库跑一遍 ``alembic upgrade head``——
这样迁移脚本本身也被纳入了验证，而不是只有模型定义被测到。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.ai.base import LLMClient
from app.ai.fake import FakeLLMClient, ScriptedResponse
from app.core.config import settings
from app.db import session as session_module
from app.db.session import get_db
from app.main import create_app
from app.services.progress import reset_chunk_bus
from app.services.storage import storage

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: 需要在测试之间清空的表，顺序无关（用 CASCADE 一并处理外键）
_TABLES = ("extraction_results", "extraction_tasks", "documents", "custom_templates")


# ============================================================================
# 数据库准备
# ============================================================================
def _run_migrations() -> None:
    """在子进程里对测试库执行迁移。

    放子进程而不是本进程的原因：alembic 的异步 env.py 内部会调用
    ``asyncio.run()``，而本进程里 pytest-asyncio 已经在跑事件循环，
    直接调用会抛 "asyncio.run() cannot be called from a running event loop"。
    """
    env = {
        **os.environ,
        # 通过环境变量把连接指向测试库。
        # config.py 里环境变量优先于 .env，子进程是全新进程，会重新读取。
        "POSTGRES_DB": settings.POSTGRES_TEST_DB,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.exit(
            "测试库迁移失败。请确认数据库已启动（make dev-db）。\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def prepared_database() -> None:
    """会话级：确保测试库结构是最新的。

    只有集成测试需要数据库，但用 autouse 让问题尽早暴露，
    而不是等到某个集成测试跑到一半才报连接错误。
    """
    try:
        engine = create_async_engine(settings.test_database_url, poolclass=NullPool)
    except Exception as exc:  # pragma: no cover
        pytest.exit(f"无法创建数据库引擎：{exc}", returncode=1)

    import asyncio

    async def _ping() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()

    try:
        asyncio.run(_ping())
    except Exception as exc:
        pytest.exit(
            f"连接测试库失败：{type(exc).__name__}: {exc}\n请先启动数据库：make dev-db",
            returncode=1,
        )

    _run_migrations()


# ============================================================================
# 数据库会话
# ============================================================================
@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """每个测试一个引擎，用 NullPool 保证测试结束连接真的关掉。

    连接池会把连接留在事件循环上，而 pytest-asyncio 每个测试换一个循环，
    复用池化的连接会报 "attached to a different loop"。
    """
    eng = create_async_engine(settings.test_database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def clean_database(engine: AsyncEngine) -> AsyncIterator[None]:
    """每个测试结束后清空数据，避免用例之间互相污染。"""
    yield
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def patch_session_factory(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """把模块级的会话工厂也指向测试库。

    请求链路可以通过 ``dependency_overrides`` 换库，但**后台任务不能**——
    ``TaskPipeline`` 与 ``TaskRunner`` 走的是模块级的 ``session_scope()``。
    不补这一刀的话，测试会往开发库里写数据，而断言查的是测试库，
    表现为"任务好像没跑"这种极难定位的现象。
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_module, "SessionFactory", factory)
    yield


# ============================================================================
# 模型客户端与 HTTP 客户端
# ============================================================================
@pytest.fixture
def llm_client() -> LLMClient:
    """默认的假模型客户端。

    **测试绝不打真实 API**——既慢又花钱，还会因为上游抖动造成随机失败。
    需要构造特定模型行为的用例，覆盖这个夹具即可：

    .. code-block:: python

        @pytest.fixture
        def llm_client():
            return FakeLLMClient([ScriptedResponse(content='{"party_a": "甲公司"}')])
    """
    return FakeLLMClient([ScriptedResponse(content="{}")])


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, llm_client: LLMClient) -> AsyncIterator[FastAPI]:
    """构造应用并把数据库指向测试库。

    调度器与模型客户端由 ``create_app`` 构造（不在 lifespan 里），
    因此这里即使不跑 lifespan 也能正常调用业务接口。
    """
    application = create_app(llm_client=llm_client)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_db] = override_get_db

    yield application

    application.dependency_overrides.clear()
    # 取消可能还在跑的后台任务，避免影响下一个用例
    await application.state.task_runner.shutdown()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """直连 ASGI 应用的测试客户端，不经过网络。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


# ============================================================================
# 上传目录隔离
# ============================================================================
@pytest.fixture(autouse=True)
def reset_shared_state() -> Iterator[None]:
    """清理跨用例的进程内共享状态。

    ``chunk_bus`` 是进程内单例，上一个用例遗留的订阅者会让计数断言失真。
    """
    yield
    reset_chunk_bus()


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把上传目录指向临时目录。

    不隔离的话，跑一次测试就会往真实的 data/uploads 里塞一堆文件，
    而且测试之间会因为 sha256 去重互相干扰。
    """
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "root", upload_dir)
    yield upload_dir


# ============================================================================
# 文件夹具
# ============================================================================
@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def contract_pdf_bytes(fixtures_dir: Path) -> bytes:
    """有文本层的中文 PDF 合同。"""
    return (fixtures_dir / "contract.pdf").read_bytes()


@pytest.fixture(scope="session")
def resume_docx_bytes(fixtures_dir: Path) -> bytes:
    """含表格的中文 Word 简历。"""
    return (fixtures_dir / "resume.docx").read_bytes()


@pytest.fixture(scope="session")
def scanned_pdf_bytes(fixtures_dir: Path) -> bytes:
    """没有文本层的 PDF，模拟扫描件。"""
    return (fixtures_dir / "scanned.pdf").read_bytes()


@pytest.fixture(scope="session")
def plain_text_bytes() -> bytes:
    return "采购合同\n甲方：北京星辰科技有限公司\n乙方：上海云图信息技术有限公司\n".encode()
