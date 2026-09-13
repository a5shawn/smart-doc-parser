"""文档服务：上传、解析、去重、查询、删除。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import anyio
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.errors import DocumentNotFoundError
from app.core.logging import get_logger
from app.db.enums import TextStatus
from app.db.models import Document, ExtractionTask
from app.services.file_validation import (
    canonical_mime_type,
    sanitize_filename,
    validate_upload,
)
from app.services.parsing import extract_text, make_preview
from app.services.storage import compute_sha256, storage

logger = get_logger(__name__)

#: 列表查询只取这些列。**必须显式列出，否则会把 text_content 一起拉出来**——
#: 一份 20 MB 的 PDF 提取出的正文可能有几十万字符，列表页拉 20 条就是几十 MB 的内存与带宽。
_LIST_COLUMNS = (
    Document.id,
    Document.filename,
    Document.stored_name,
    Document.mime_type,
    Document.size_bytes,
    Document.sha256,
    Document.page_count,
    Document.char_count,
    Document.text_preview,
    Document.text_status,
    Document.text_error,
    Document.created_at,
)

#: 扫描件的说明文案。这不是系统故障，措辞要让用户知道下一步能做什么。
NO_TEXT_LAYER_MESSAGE = (
    "未检测到文本层，可能是扫描件或纯图片 PDF。请上传电子版文档，或等待后续的 OCR 支持。"
)


def _task_count_subquery():
    """相关子查询：统计每份文档下的任务数。

    用子查询而不是逐个文档再查一次，是为了避免列表接口出现 N+1。
    """
    return (
        select(func.count(ExtractionTask.id))
        .where(ExtractionTask.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


async def create_document(
    db: AsyncSession,
    *,
    filename: str,
    data: bytes,
) -> tuple[Document, bool]:
    """校验、解析并保存一份上传文件。

    :returns: ``(文档, 是否为重复文件)``。重复时返回已存在的记录，不重复落盘。

    :raises FileValidationError: 扩展名/大小/文件头校验未通过
    :raises ParsingError: 文件损坏或格式不受支持
    """
    safe_filename = sanitize_filename(filename)
    extension = validate_upload(safe_filename, data)
    sha256 = compute_sha256(data)

    # ---- 去重：同一份文件重复上传不应存两份 ----
    existing = await db.scalar(select(Document).where(Document.sha256 == sha256))
    if existing is not None:
        logger.info("duplicate_document", document_id=str(existing.id), sha256=sha256[:12])
        return existing, True

    # ---- 解析 ----
    # extract_text 是 CPU 密集的，放到线程池执行，避免阻塞事件循环
    # （pypdfium2 的 C 层会释放 GIL，放线程里是实打实的并行）
    parsed = await anyio.to_thread.run_sync(extract_text, safe_filename, data)

    has_text = parsed.has_text_layer
    # ---- 落盘 ----
    stored_name = await storage.save(data, extension)

    document = Document(
        filename=safe_filename,
        stored_name=stored_name,
        mime_type=canonical_mime_type(extension),
        size_bytes=len(data),
        sha256=sha256,
        page_count=parsed.page_count,
        char_count=parsed.char_count,
        # 扫描件存 None 而不是空串，让"没有正文"和"正文为空"在数据库里可区分
        text_content=parsed.text or None,
        text_preview=make_preview(parsed.text) or None,
        text_status=TextStatus.EXTRACTED if has_text else TextStatus.NO_TEXT_LAYER,
        text_error=None if has_text else NO_TEXT_LAYER_MESSAGE,
    )
    db.add(document)

    try:
        await db.commit()
    except IntegrityError:
        # 并发上传同一份文件时，两个请求可能都通过了上面的去重检查。
        # 这里靠数据库的唯一约束兜底：回滚、删掉刚写的文件、复用已有记录。
        await db.rollback()
        await storage.delete(stored_name)
        existing = await db.scalar(select(Document).where(Document.sha256 == sha256))
        if existing is not None:
            logger.info("duplicate_document_race", document_id=str(existing.id))
            return existing, True
        raise

    await db.refresh(document)
    logger.info(
        "document_created",
        document_id=str(document.id),
        filename=safe_filename,
        size_bytes=len(data),
        char_count=parsed.char_count,
        text_status=document.text_status.value,
    )
    return document, False


def _base_list_query() -> Select:
    return select(Document, _task_count_subquery().label("task_count")).options(
        load_only(*_LIST_COLUMNS)
    )


async def list_documents(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[Sequence[tuple[Document, int]], int]:
    """分页查询文档，按创建时间倒序。

    :returns: ``([(文档, 任务数), ...], 总数)``
    """
    total = await db.scalar(select(func.count()).select_from(Document)) or 0

    result = await db.execute(
        _base_list_query()
        .order_by(Document.created_at.desc(), Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return [(row[0], row[1]) for row in result.all()], total


async def get_document(db: AsyncSession, document_id: UUID) -> Document:
    """按 ID 取文档（含正文）。"""
    document = await db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(details={"document_id": str(document_id)})
    return document


async def get_document_with_task_count(db: AsyncSession, document_id: UUID) -> tuple[Document, int]:
    result = await db.execute(
        select(Document, _task_count_subquery().label("task_count")).where(
            Document.id == document_id
        )
    )
    row = result.first()
    if row is None:
        raise DocumentNotFoundError(details={"document_id": str(document_id)})
    return row[0], row[1]


async def delete_document(db: AsyncSession, document_id: UUID) -> int:
    """删除文档及其所有任务与结果。

    外键上是 ``ON DELETE CASCADE``，因此任务与结果会被数据库一并删除，
    无需应用层逐个清理。**这是不可恢复的硬删除**，前端必须明确提示影响范围。

    :returns: 被连带删除的任务数，用于向用户回执
    """
    document = await get_document(db, document_id)
    task_count = (
        await db.scalar(
            select(func.count(ExtractionTask.id)).where(ExtractionTask.document_id == document_id)
        )
        or 0
    )

    stored_name = document.stored_name
    await db.delete(document)
    await db.commit()

    # 数据库记录删掉后再删文件。反过来的话，如果数据库操作失败，
    # 就会出现"记录在、文件没了"的坏状态，列表点进去直接报错。
    await storage.delete(stored_name)

    logger.info(
        "document_deleted",
        document_id=str(document_id),
        cascaded_tasks=task_count,
    )
    return task_count
