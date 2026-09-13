"""健康检查。

**刻意分成两个端点**，因为它们的语义和调用方完全不同：

- ``/health/live``  存活探针：进程还在跑吗？**不检查任何外部依赖**。
  如果这里去连数据库，数据库抖动会导致编排系统不停地重启一个本来健康的容器，
  把一次小故障放大成雪崩。
- ``/health/ready`` 就绪探针：现在能正常服务请求吗？会真的查一次数据库。
  未就绪时编排系统会把流量摘掉，但**不会**重启容器。

这两个端点永不鉴权——否则健康检查会一直返回 401，容器永远起不来。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger
from app.db.session import check_database

logger = get_logger(__name__)

router = APIRouter(prefix="/health", tags=["健康检查"])


class LivenessOut(BaseModel):
    status: str
    app: str
    version: str
    environment: str


class ReadinessOut(BaseModel):
    status: str
    database: str


@router.get("/live", response_model=LivenessOut, summary="存活探针")
async def liveness() -> LivenessOut:
    """只证明进程活着，不碰数据库。"""
    return LivenessOut(
        status="ok",
        app=settings.APP_NAME,
        version=__version__,
        environment=settings.APP_ENV,
    )


@router.get("/ready", response_model=ReadinessOut, summary="就绪探针")
async def readiness() -> ReadinessOut:
    """确认依赖可用。数据库连不上时返回 503，让编排系统把流量摘掉。"""
    try:
        await check_database()
    except Exception as exc:
        logger.error("readiness_check_failed", error=str(exc), exc_info=True)
        raise ServiceUnavailableError(
            "数据库连接失败，服务暂不可用",
            details={"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc

    return ReadinessOut(status="ok", database="ok")
