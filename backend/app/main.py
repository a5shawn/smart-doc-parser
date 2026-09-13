"""应用入口。

``create_app()`` 是工厂函数而不是模块级单例，好处是测试可以针对不同配置
构造独立的应用实例，且导入本模块**不会**连接数据库——这让
``scripts/export_openapi.py`` 能在无数据库环境下生成接口文档。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from app import __version__
from app.ai.base import LLMClient
from app.ai.deepseek_client import create_llm_client
from app.ai.extractor import Extractor
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.db.session import dispose_engine
from app.services.storage import storage
from app.services.task_pipeline import TaskPipeline
from app.services.task_runner import TaskRunner

#: 所有业务接口的统一前缀。前端通过相对路径 /api/v1 访问，
#: 这样 nginx 反代（线上）与 Vite proxy（本地）行为完全一致。
API_V1_PREFIX = "/api/v1"

logger = get_logger(__name__)

DESCRIPTION = """
把 PDF / Word 文档丢进来，用大模型抽取成结构化 JSON。

- **多模板**：内置合同、简历、发票三套模板，也支持自定义 JSON Schema
- **实时进度**：通过 SSE 推送解析与抽取进度，可看到模型逐字生成的内容
- **成本可见**：每个任务都记录 token 用量与实际花费
"""


def _to_camel_case(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def custom_generate_unique_id(route: APIRoute) -> str:
    """生成简洁的 operationId，例如 ``listDocuments``。

    默认实现是 ``list_documents_api_v1_documents_get`` 这种把路径拼进去的长名字，
    它会被 openapi-typescript 原样带进前端类型定义——路径一改，前端所有引用全断。
    用函数名（驼峰化）做标识，接口路径调整时前端类型不受影响。
    """
    return _to_camel_case(route.name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动与关闭时的一次性工作。"""
    configure_logging()

    logger.info(
        "application_starting",
        app=settings.APP_NAME,
        version=__version__,
        environment=settings.APP_ENV,
        model=settings.DEEPSEEK_MODEL,
        auth_enabled=settings.AUTH_ENABLED,
    )

    # 配置风险提示：不阻止启动，但必须让运维看见
    for warning in settings.startup_warnings:
        logger.warning("configuration_warning", detail=warning)

    # 上传目录可能不存在（首次部署、挂载了空数据卷）
    await storage.ensure_root()

    # 上次进程退出时残留的「进行中」任务不可能再有人推进了，标记为失败让用户可重试。
    # 不做这一步，前端进度条会永远卡在那里，用户不知道该等还是该重试。
    recovered = await app.state.task_runner.recover_interrupted_tasks()
    if recovered:
        logger.warning("startup_recovered_interrupted_tasks", count=recovered)

    yield

    logger.info("application_stopping")
    # 先让在途任务收尾，再关模型客户端——反过来的话收尾中的任务会撞上已关闭的连接
    await app.state.task_runner.shutdown()
    await app.state.llm_client.aclose()
    await dispose_engine()


def create_app(*, llm_client: LLMClient | None = None) -> FastAPI:
    """构造应用。

    :param llm_client: 注入自定义的模型客户端。测试传入假实现，
        生产环境留空则按配置构造真实的 DeepSeek 客户端。

    调度器与模型客户端在这里构造（而不是在 lifespan 里），
    这样测试用 ASGITransport 直接调用应用时它们也是可用的——
    ASGITransport 默认不会执行 lifespan。
    """
    client = llm_client or create_llm_client()
    runner = TaskRunner(
        pipeline=TaskPipeline(
            extractor=Extractor(client), max_input_chars=settings.MAX_INPUT_CHARS
        ),
        max_concurrent=settings.MAX_CONCURRENT_TASKS,
        timeout_seconds=settings.TASK_TIMEOUT_SECONDS,
    )

    app = FastAPI(
        title="智能文档解析服务",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        generate_unique_id_function=custom_generate_unique_id,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # 生产环境可以关掉文档，避免把接口细节暴露给所有人
        openapi_tags=[
            {"name": "健康检查", "description": "存活与就绪探针，供容器编排使用"},
            {"name": "文档", "description": "上传、查询与删除文档"},
            {"name": "任务", "description": "创建抽取任务、查询进度与结果"},
            {"name": "模板", "description": "内置与自定义抽取模板"},
            {"name": "统计", "description": "用量与成本统计"},
        ],
    )

    app.state.llm_client = client
    app.state.task_runner = runner

    # 中间件注册顺序与执行顺序相反：最后注册的最先执行。
    # 这里希望 RequestContextMiddleware 最先执行（拿到最早的计时点），
    # 因此它要在 CORS 之后注册。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        # 我们用的是 X-API-Key 请求头而不是 Cookie，不需要凭据模式。
        # 而且 allow_credentials=True 与 allow_origins=["*"] 组合会被浏览器直接拒绝。
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_V1_PREFIX)

    return app


app = create_app()
