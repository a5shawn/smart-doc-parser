"""ORM 模型。

四张表：
- ``documents``          上传的文件及其提取出的纯文本
- ``extraction_tasks``   一次「用某模板抽取某文档」的任务，含状态机与用量统计
- ``extraction_results`` 抽取出的结构化数据与模型原始输出
- ``custom_templates``   用户自定义的 JSON Schema 模板
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import TaskStatus, TextStatus


def _pg_enum(enum_cls: type, name: str) -> SAEnum:
    """构造 PostgreSQL 原生枚举列。

    ``values_callable`` 让数据库里存的是枚举的 **值**（小写）而不是成员名（大写），
    这样 SQL 里看到的是 ``'completed'`` 而不是 ``'COMPLETED'``，与 API 输出一致。
    """
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [member.value for member in cls],
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
    )


# ============================================================================
# documents
# ============================================================================
class Document(Base, TimestampMixin):
    """上传的文档及其解析出的纯文本。"""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        # PG 13+ 内置，无需 CREATE EXTENSION pgcrypto
        server_default=func.gen_random_uuid(),
    )

    #: 用户上传时的原始文件名。**仅用于展示**，绝不参与磁盘路径拼接。
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 磁盘上的实际文件名，形如 ``<uuid4>.pdf``，由服务端生成
    stored_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 内容指纹，用于去重：同一份文件重复上传会命中已有记录而不是存两份
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    page_count: Mapped[int | None] = mapped_column(Integer)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: 提取出的完整纯文本。体积可能很大，列表查询务必不要 SELECT 这一列（见 document_service）
    text_content: Mapped[str | None] = mapped_column(Text)
    #: 前 500 字，列表页预览用，避免为了展示一行而拉取整个正文
    text_preview: Mapped[str | None] = mapped_column(String(500))

    text_status: Mapped[TextStatus] = mapped_column(
        _pg_enum(TextStatus, "text_status"),
        nullable=False,
        default=TextStatus.EXTRACTED,
        server_default=TextStatus.EXTRACTED.value,
    )
    #: text_status 为 failed 时的原因
    text_error: Mapped[str | None] = mapped_column(String(500))

    tasks: Mapped[list[ExtractionTask]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_documents_created_at", "created_at"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
    )


# ============================================================================
# extraction_tasks
# ============================================================================
class ExtractionTask(Base, TimestampMixin):
    """一次抽取任务：某份文档 + 某个模板。"""

    __tablename__ = "extraction_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: 内置模板 key（contract/resume/invoice）或自定义模板 id
    template_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 模板名的快照。模板被删除后历史任务仍能显示当时用的是哪个模板
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 自定义模板的 schema 快照。存快照而非外键，保证模板被删也能复现当时的结果
    custom_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: 同一批上传创建的多个任务共享一个 batch_id，前端据此聚合展示
    batch_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    status: Mapped[TaskStatus] = mapped_column(
        _pg_enum(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stage_message: Mapped[str | None] = mapped_column(String(128))

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))

    # ---------------------------- 用量与成本 ----------------------------
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: 推理模型的思考 token，已包含在 completion_tokens 内，单独记一份便于分析
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: 命中前缀缓存的输入 token，可用来量化 prompt 组织策略的效果
    cached_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="tasks")
    result: Mapped[ExtractionResult | None] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        # 任务列表默认按状态过滤 + 时间倒序，组合索引正好覆盖
        Index("ix_extraction_tasks_status_created_at", "status", "created_at"),
        Index("ix_extraction_tasks_batch_id", "batch_id"),
        Index("ix_extraction_tasks_template_key", "template_key"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="progress_range"),
    )


# ============================================================================
# extraction_results
# ============================================================================
class ExtractionResult(Base, TimestampMixin):
    """抽取结果。与任务一对一。"""

    __tablename__ = "extraction_results"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("extraction_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    #: 校验并规范化后的结构化数据，前端按模板字段渲染卡片
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: 模型的原始返回。**保留它才能在结果可疑时回溯是模型输出问题还是解析问题**
    raw_output: Mapped[str] = mapped_column(Text, nullable=False)
    #: 非致命问题清单（字段缺失、类型被规范化、文档被截断等）
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: 输入是否因为超过 MAX_INPUT_CHARS 被截断
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    task: Mapped[ExtractionTask] = relationship(back_populates="result")


# ============================================================================
# custom_templates
# ============================================================================
class CustomTemplate(Base, TimestampMixin):
    """用户自定义的 JSON Schema 模板。"""

    __tablename__ = "custom_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    #: 标准 JSON Schema（Draft 2020-12）
    schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: 附加的抽取提示，例如"金额请统一换算为人民币元"
    prompt_hint: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (Index("ix_custom_templates_created_at", "created_at"),)
