"""文档相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import TextStatus


class DocumentOut(BaseModel):
    """文档摘要。不含正文——列表页拉 20 份文档的全文纯属浪费带宽。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str = Field(description="用户上传时的原始文件名，仅用于展示")
    mime_type: str
    size_bytes: int
    sha256: str = Field(description="内容指纹，可用于判断是否为同一份文件")
    page_count: int | None
    char_count: int = Field(description="提取到的文本字符数")
    text_preview: str | None = Field(description="正文前 500 字，供列表预览")
    text_status: TextStatus
    text_error: str | None = Field(default=None, description="提取失败时的原因说明")
    task_count: int = Field(default=0, description="该文档下的抽取任务数，删除前提示用")
    created_at: datetime

    @classmethod
    def from_row(cls, document, task_count: int = 0) -> Self:
        """由 ORM 对象与任务计数构造。

        ``task_count`` 由调用方用一次聚合查询批量算出，避免 N+1。
        它不在 ORM 模型上，因此先按属性映射出其余字段再补这一项。
        """
        data = cls.model_validate(document)
        data.task_count = task_count
        return data


class DocumentDetailOut(DocumentOut):
    """文档详情。额外带上提取出的正文，便于排查抽取质量问题。

    复用父类的 ``from_row``：``cls`` 在这里是 ``DocumentDetailOut``，
    因此会连同 ``text_content`` 一起映射出来。
    """

    text_content: str | None = Field(default=None, description="提取出的完整纯文本")


class RejectedUpload(BaseModel):
    """批量上传中被拒绝的文件。"""

    filename: str
    code: str
    message: str


class BatchUploadResponse(BaseModel):
    """批量上传结果。

    采用「部分成功」语义：一个坏文件不会让整批回滚。
    用户拖 20 个文件进来时，因为其中一个格式不对就全军覆没是最反直觉的行为。
    """

    accepted: list[DocumentOut] = Field(default_factory=list, description="新入库的文档")
    duplicate: list[DocumentOut] = Field(
        default_factory=list, description="内容已存在，直接复用已有记录"
    )
    rejected: list[RejectedUpload] = Field(default_factory=list, description="校验未通过的文件")

    @property
    def has_any_success(self) -> bool:
        return bool(self.accepted or self.duplicate)
