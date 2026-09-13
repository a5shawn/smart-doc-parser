"""抽取任务接口。

:warning: ``/tasks/stream``（SSE）定义在 ``task_stream.py`` 里，
且必须在 ``router.py`` 中**先于本模块注册**——否则 ``/tasks/{task_id}``
会把 ``stream`` 当成 UUID 解析并返回 422。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, PaginationDep, TaskRunnerDep
from app.core.logging import get_logger
from app.db.enums import TaskStatus
from app.schemas.common import Page
from app.schemas.task import (
    ExtractionResultOut,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDeleteResponse,
    TaskOut,
)
from app.services import task_service, template_service

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["任务"])


@router.post(
    "",
    response_model=TaskCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建抽取任务",
    description=(
        "为一批文档创建抽取任务并立即交给后台执行，**接口立刻返回不等待结果**。\n\n"
        "进度通过 `GET /tasks/stream`（SSE）推送，或轮询 `GET /tasks/{id}`。\n\n"
        "同一份文档可以用不同模板反复抽取，不需要重新上传文件。"
    ),
)
async def create_tasks(
    db: DbSession,
    runner: TaskRunnerDep,
    payload: TaskCreateRequest,
) -> TaskCreateResponse:
    template = await template_service.get_template(db, payload.template_key)
    batch_id = payload.batch_id or uuid4()

    tasks = await task_service.create_tasks(
        db,
        document_ids=payload.document_ids,
        template=template,
        batch_id=batch_id,
    )

    # 创建完就交给后台，接口不等待模型调用——这正是异步任务架构的意义
    await runner.submit([task.id for task in tasks])

    return TaskCreateResponse(
        batch_id=batch_id,
        tasks=[TaskOut.from_row(task) for task in tasks],
    )


@router.get("", response_model=Page[TaskOut], summary="任务列表")
async def list_tasks(
    db: DbSession,
    pagination: PaginationDep,
    task_status: TaskStatus | None = Query(default=None, alias="status", description="按状态过滤"),
    template_key: str | None = Query(default=None, description="按模板过滤"),
    batch_id: UUID | None = Query(default=None, description="按批次过滤"),
) -> Page[TaskOut]:
    rows, total = await task_service.list_tasks(
        db,
        page=pagination.page,
        page_size=pagination.page_size,
        status=task_status,
        template_key=template_key,
        batch_id=batch_id,
    )
    return Page[TaskOut](
        items=[TaskOut.from_row(task, filename) for task, filename in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{task_id}", response_model=TaskOut, summary="任务详情")
async def get_task(db: DbSession, task_id: UUID) -> TaskOut:
    task, filename = await task_service.get_task_with_filename(db, task_id)
    return TaskOut.from_row(task, filename)


@router.post(
    "/{task_id}/retry",
    response_model=TaskOut,
    summary="重新抽取",
    description=(
        "把失败或已完成的任务重置为待执行并重新排队。\n\n"
        "**会删除上一次的抽取结果**——不删的话，重跑失败时界面上会出现"
        "「任务失败但结果还在」的矛盾状态。"
    ),
)
async def retry_task(db: DbSession, runner: TaskRunnerDep, task_id: UUID) -> TaskOut:
    task = await task_service.prepare_retry(db, task_id)
    await runner.submit([task.id])
    return TaskOut.from_row(task)


@router.delete("/{task_id}", response_model=TaskDeleteResponse, summary="删除任务")
async def delete_task(db: DbSession, task_id: UUID) -> TaskDeleteResponse:
    await task_service.delete_task(db, task_id)
    return TaskDeleteResponse(deleted=True)


@router.get(
    "/{task_id}/result",
    response_model=ExtractionResultOut,
    summary="获取抽取结果",
    description=(
        "返回校验并规范化后的结构化数据，以及模型原始输出。\n\n"
        "`warnings` 是非致命问题清单（字段缺失、类型被矫正、文档被截断等），"
        "用户可据此判断结果是否需要人工复核。"
    ),
)
async def get_result(db: DbSession, task_id: UUID) -> ExtractionResultOut:
    result = await task_service.get_result(db, task_id)
    return ExtractionResultOut.from_row(result)
