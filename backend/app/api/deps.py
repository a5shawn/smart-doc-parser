"""FastAPI 依赖注入。

把这些类型别名集中定义，路由签名可以写得更短、更一致：
``async def list_documents(db: DbSession, pagination: PaginationDep)``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import FileTooLargeError
from app.core.security import verify_api_key
from app.db.session import get_db
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.services.task_runner import TaskRunner

#: 数据库会话
DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_task_runner(request: Request) -> TaskRunner:
    """取当前应用的调度器实例。

    从 ``app.state`` 取而不是用模块级单例：测试可以针对每个应用实例
    构造独立的调度器，也能注入假的模型客户端。
    """
    return request.app.state.task_runner


#: 任务调度器
TaskRunnerDep = Annotated[TaskRunner, Depends(get_task_runner)]

#: 需要鉴权的接口挂上这个依赖。``AUTH_ENABLED=false`` 时它是空操作。
AuthRequired = Annotated[None, Depends(verify_api_key)]


class Pagination:
    """分页参数。上限 ``MAX_PAGE_SIZE`` 是硬约束，防止调用方用超大 page_size 拖垮数据库。"""

    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
        page_size: Annotated[
            int,
            Query(ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}"),
        ] = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


PaginationDep = Annotated[Pagination, Depends()]

#: 每次从上传流里读取的块大小
UPLOAD_CHUNK_SIZE = 1024 * 1024


async def read_upload_within_limit(upload: UploadFile) -> bytes:
    """分块读取上传内容，超限立即中断。

    为什么不直接 ``await upload.read()``：那样会先把整个文件读完才发现太大，
    攻击者可以用一个超大文件把磁盘写满。分块读取能在超出上限的瞬间就停下来。

    注意这是**第二道**防线——nginx 的 ``client_max_body_size`` 会更早地拒掉超大请求，
    但那层只在容器部署时存在，直接访问后端端口时不起作用。
    """
    limit = settings.max_upload_size_bytes
    chunks: list[bytes] = []
    total = 0

    while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(
                f"文件大小超过 {settings.MAX_UPLOAD_SIZE_MB} MB 上限",
                details={
                    "filename": upload.filename,
                    "limit_bytes": limit,
                    "limit_mb": settings.MAX_UPLOAD_SIZE_MB,
                },
            )
        chunks.append(chunk)

    return b"".join(chunks)
