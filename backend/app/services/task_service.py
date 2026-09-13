"""抽取任务的增删查与统计。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.ai.templates import ExtractionTemplate
from app.core.errors import ConflictError, ResultNotFoundError, TaskNotFoundError
from app.core.logging import get_logger
from app.db.enums import TaskStatus
from app.db.models import Document, ExtractionResult, ExtractionTask

logger = get_logger(__name__)

#: 任务列表要连文档名一起展示，但**不拉正文**——几十份文档的全文会让响应膨胀几十 MB
_TASK_LIST_COLUMNS = (
    ExtractionTask.id,
    ExtractionTask.document_id,
    ExtractionTask.template_key,
    ExtractionTask.template_name,
    ExtractionTask.batch_id,
    ExtractionTask.status,
    ExtractionTask.progress,
    ExtractionTask.stage_message,
    ExtractionTask.error_code,
    ExtractionTask.error_message,
    ExtractionTask.model,
    ExtractionTask.prompt_tokens,
    ExtractionTask.completion_tokens,
    ExtractionTask.reasoning_tokens,
    ExtractionTask.cached_tokens,
    ExtractionTask.cost_usd,
    ExtractionTask.latency_ms,
    ExtractionTask.attempts,
    ExtractionTask.started_at,
    ExtractionTask.finished_at,
    ExtractionTask.created_at,
)

_DOCUMENT_LIST_COLUMNS = (
    Document.id,
    Document.filename,
    Document.size_bytes,
    Document.page_count,
    Document.char_count,
    Document.text_status,
)


async def create_tasks(
    db: AsyncSession,
    *,
    document_ids: Sequence[UUID],
    template: ExtractionTemplate,
    batch_id: UUID,
) -> list[ExtractionTask]:
    """为一批文档创建抽取任务。"""
    documents = (
        (
            await db.execute(
                select(Document)
                .where(Document.id.in_(document_ids))
                .options(load_only(*_DOCUMENT_LIST_COLUMNS))
            )
        )
        .scalars()
        .all()
    )

    found_ids = {document.id for document in documents}
    missing = [str(item) for item in document_ids if item not in found_ids]
    if missing:
        raise TaskNotFoundError(
            "部分文档不存在或已被删除",
            details={"missing_document_ids": missing},
        )

    # 按入参顺序构造，方便调用方把返回的任务与上传的文件一一对上号
    tasks = [
        ExtractionTask(
            document_id=document_id,
            template_key=template.key,
            # 存模板名快照：模板日后被改名或删除，历史任务仍能正确显示
            template_name=template.name,
            # 自定义模板存 schema 快照，重跑时能复现当时的行为
            custom_schema=template.schema if not template.builtin else None,
            batch_id=batch_id,
            status=TaskStatus.PENDING,
        )
        for document_id in document_ids
    ]

    db.add_all(tasks)
    await db.commit()
    for task in tasks:
        await db.refresh(task)

    logger.info(
        "tasks_created",
        count=len(tasks),
        template_key=template.key,
        batch_id=str(batch_id),
    )
    return tasks


def _base_list_query() -> Select:
    return (
        select(ExtractionTask, Document.filename)
        .join(Document, Document.id == ExtractionTask.document_id)
        .options(load_only(*_TASK_LIST_COLUMNS))
    )


async def list_tasks(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    status: TaskStatus | None = None,
    template_key: str | None = None,
    batch_id: UUID | None = None,
) -> tuple[list[tuple[ExtractionTask, str]], int]:
    """分页查询任务。

    :returns: ``([(任务, 文档名), ...], 总数)``
    """
    conditions = []
    if status is not None:
        conditions.append(ExtractionTask.status == status)
    if template_key is not None:
        conditions.append(ExtractionTask.template_key == template_key)
    if batch_id is not None:
        conditions.append(ExtractionTask.batch_id == batch_id)

    count_query = select(func.count(ExtractionTask.id)).join(
        Document, Document.id == ExtractionTask.document_id
    )
    if conditions:
        count_query = count_query.where(*conditions)
    total = await db.scalar(count_query) or 0

    query = _base_list_query()
    if conditions:
        query = query.where(*conditions)
    query = (
        query.order_by(ExtractionTask.created_at.desc(), ExtractionTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    rows = (await db.execute(query)).all()
    return [(row[0], row[1]) for row in rows], total


async def get_task(db: AsyncSession, task_id: UUID) -> ExtractionTask:
    task = await db.get(ExtractionTask, task_id)
    if task is None:
        raise TaskNotFoundError(details={"task_id": str(task_id)})
    return task


async def get_task_with_filename(db: AsyncSession, task_id: UUID) -> tuple[ExtractionTask, str]:
    row = (
        await db.execute(
            select(ExtractionTask, Document.filename)
            .join(Document, Document.id == ExtractionTask.document_id)
            .where(ExtractionTask.id == task_id)
            .options(load_only(*_TASK_LIST_COLUMNS))
        )
    ).first()

    if row is None:
        raise TaskNotFoundError(details={"task_id": str(task_id)})
    return row[0], row[1]


async def get_result(db: AsyncSession, task_id: UUID) -> ExtractionResult:
    """取任务的抽取结果。"""
    task = await get_task(db, task_id)

    result = await db.scalar(select(ExtractionResult).where(ExtractionResult.task_id == task_id))
    if result is None:
        # 区分"任务不存在"和"任务存在但还没结果"，前端据此决定提示什么
        if task.status is TaskStatus.FAILED:
            raise ResultNotFoundError(
                "任务执行失败，没有抽取结果",
                details={"task_id": str(task_id), "error_code": task.error_code},
            )
        raise ResultNotFoundError(
            "任务尚未完成，暂时没有结果",
            details={"task_id": str(task_id), "status": task.status.value},
        )
    return result


async def prepare_retry(db: AsyncSession, task_id: UUID) -> ExtractionTask:
    """把任务重置为待执行，供重新抽取使用。

    **会删除上一次的结果**：不删的话，重跑失败时旧结果还留在库里，
    界面上会出现"任务失败但结果还在"的矛盾状态。
    """
    task = await get_task(db, task_id)

    if not task.status.is_terminal:
        raise ConflictError(
            "任务正在进行中，无法重复提交",
            details={"task_id": str(task_id), "status": task.status.value},
        )

    existing = await db.scalar(select(ExtractionResult).where(ExtractionResult.task_id == task_id))
    if existing is not None:
        await db.delete(existing)

    task.status = TaskStatus.PENDING
    task.progress = 0
    task.stage_message = None
    task.error_code = None
    task.error_message = None
    task.started_at = None
    task.finished_at = None
    task.latency_ms = None
    await db.commit()
    await db.refresh(task)

    logger.info("task_prepared_for_retry", task_id=str(task_id))
    return task


async def delete_task(db: AsyncSession, task_id: UUID) -> None:
    task = await get_task(db, task_id)
    await db.delete(task)
    await db.commit()
    logger.info("task_deleted", task_id=str(task_id))


# ============================================================================
# 统计
# ============================================================================
@dataclass(frozen=True, slots=True)
class TaskStats:
    """全局统计。用于回答"这个功能到底花了多少钱、跑得怎么样"。"""

    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    running_tasks: int
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    total_reasoning_tokens: int
    total_cached_tokens: int
    avg_latency_ms: float | None
    total_documents: int

    @property
    def success_rate(self) -> float:
        """成功率。

        只统计**已结束**的任务——把还在排队的算进分母会让成功率虚低，
        用户看到"成功率 20%"会以为系统有问题，实际只是任务还没跑完。
        """
        finished = self.completed_tasks + self.failed_tasks
        if finished == 0:
            return 0.0
        return round(self.completed_tasks / finished, 4)


async def get_stats(db: AsyncSession) -> TaskStats:
    task_row = (
        await db.execute(
            select(
                func.count(ExtractionTask.id),
                func.count(ExtractionTask.id).filter(ExtractionTask.status == TaskStatus.COMPLETED),
                func.count(ExtractionTask.id).filter(ExtractionTask.status == TaskStatus.FAILED),
                func.count(ExtractionTask.id).filter(
                    ExtractionTask.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED])
                ),
                func.coalesce(func.sum(ExtractionTask.cost_usd), 0.0),
                func.coalesce(func.sum(ExtractionTask.prompt_tokens), 0),
                func.coalesce(func.sum(ExtractionTask.completion_tokens), 0),
                func.coalesce(func.sum(ExtractionTask.reasoning_tokens), 0),
                func.coalesce(func.sum(ExtractionTask.cached_tokens), 0),
                func.avg(ExtractionTask.latency_ms),
            )
        )
    ).one()

    total_documents = await db.scalar(select(func.count(Document.id))) or 0

    return TaskStats(
        total_tasks=task_row[0],
        completed_tasks=task_row[1],
        failed_tasks=task_row[2],
        running_tasks=task_row[3],
        total_cost_usd=round(float(task_row[4]), 8),
        total_prompt_tokens=task_row[5],
        total_completion_tokens=task_row[6],
        total_reasoning_tokens=task_row[7],
        total_cached_tokens=task_row[8],
        avg_latency_ms=round(float(task_row[9]), 1) if task_row[9] is not None else None,
        total_documents=total_documents,
    )
