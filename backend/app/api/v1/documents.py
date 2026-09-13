"""文档接口：上传、列表、详情、删除。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import DbSession, PaginationDep, read_upload_within_limit
from app.core.errors import AppError
from app.core.logging import get_logger
from app.schemas.common import Page
from app.schemas.document import (
    BatchUploadResponse,
    DocumentDetailOut,
    DocumentOut,
    RejectedUpload,
)
from app.services import document_service
from app.services.file_validation import sanitize_filename

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["文档"])


class DocumentDeleteResponse(BaseModel):
    deleted: bool
    cascaded_tasks: int = Field(description="被连带删除的抽取任务数")


@router.post(
    "",
    response_model=BatchUploadResponse,
    summary="上传文档（1 份或多份）",
    description=(
        "接收 1~N 个文件，逐个校验、解析并入库。\n\n"
        "**部分成功语义**：单个文件失败不会让整批回滚，失败项会出现在 `rejected` 中。\n"
        "内容完全相同的文件不会重复存储，会出现在 `duplicate` 中并复用已有记录。\n\n"
        "状态码固定为 200：请求本身处理成功了，每个文件的结果在响应体里分别给出。"
        "用单一状态码表达「3 个成功 1 个失败」既做不到也不直观。"
    ),
)
async def upload_documents(
    db: DbSession,
    files: Annotated[
        list[UploadFile],
        File(description="待上传的文件，支持 .pdf / .docx / .txt / .md"),
    ],
) -> BatchUploadResponse:
    if not files:
        return BatchUploadResponse()

    result = BatchUploadResponse()

    for upload in files:
        raw_filename = upload.filename or "unnamed"
        display_name = sanitize_filename(raw_filename)

        try:
            data = await read_upload_within_limit(upload)
            document, is_duplicate = await document_service.create_document(
                db, filename=raw_filename, data=data
            )
            payload = DocumentOut.from_row(document)
            if is_duplicate:
                result.duplicate.append(payload)
            else:
                result.accepted.append(payload)

        except AppError as exc:
            # 业务异常：预期内的失败（格式不对、太大、损坏等），记一条日志即可
            await db.rollback()
            logger.warning(
                "upload_rejected",
                filename=display_name,
                code=exc.code,
                message=exc.message,
            )
            result.rejected.append(
                RejectedUpload(filename=display_name, code=exc.code, message=exc.message)
            )

        except Exception as exc:
            # 非预期异常：必须带堆栈记日志，但同样不能拖垮整批
            await db.rollback()
            logger.error(
                "upload_unexpected_error",
                filename=display_name,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            result.rejected.append(
                RejectedUpload(
                    filename=display_name,
                    code="INTERNAL_ERROR",
                    message="处理该文件时发生未预期的错误，请重试或联系管理员",
                )
            )

        finally:
            # 及时释放临时文件句柄，否则批量上传大文件时磁盘会堆积
            await upload.close()

    logger.info(
        "batch_upload_finished",
        accepted=len(result.accepted),
        duplicate=len(result.duplicate),
        rejected=len(result.rejected),
    )
    return result


@router.get("", response_model=Page[DocumentOut], summary="文档列表")
async def list_documents(db: DbSession, pagination: PaginationDep) -> Page[DocumentOut]:
    rows, total = await document_service.list_documents(
        db, page=pagination.page, page_size=pagination.page_size
    )
    return Page[DocumentOut](
        items=[DocumentOut.from_row(document, task_count) for document, task_count in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{document_id}", response_model=DocumentDetailOut, summary="文档详情")
async def get_document(db: DbSession, document_id: UUID) -> DocumentDetailOut:
    document, task_count = await document_service.get_document_with_task_count(db, document_id)
    return DocumentDetailOut.from_row(document, task_count)


@router.delete(
    "/{document_id}",
    response_model=DocumentDeleteResponse,
    summary="删除文档",
    description=(
        "**不可恢复的硬删除**。外键上是 ON DELETE CASCADE，"
        "该文档下的所有抽取任务与结果会一并被数据库删除。"
    ),
)
async def delete_document(db: DbSession, document_id: UUID) -> DocumentDeleteResponse:
    cascaded = await document_service.delete_document(db, document_id)
    return DocumentDeleteResponse(deleted=True, cascaded_tasks=cascaded)
