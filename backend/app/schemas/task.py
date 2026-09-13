"""抽取任务的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai.pricing import usd_to_cny
from app.db.enums import TaskStatus

#: 单次最多创建的抽取任务数。防止一次请求把几百个任务塞进队列，
#: 把并发的模型调用打满、触发上游限流。
MAX_TASKS_PER_REQUEST = 50


class TaskCreateRequest(BaseModel):
    document_ids: list[UUID] = Field(
        min_length=1,
        max_length=MAX_TASKS_PER_REQUEST,
        description=f"要抽取的文档 ID 列表，1~{MAX_TASKS_PER_REQUEST} 个",
    )
    template_key: str = Field(
        description="模板标识：内置模板用 contract / resume / invoice，自定义模板用 custom:<uuid>"
    )
    batch_id: UUID | None = Field(
        default=None,
        description="批次 ID。不传则自动生成；同一批上传的文档传入相同值便于聚合展示",
    )

    @field_validator("document_ids")
    @classmethod
    def _deduplicate(cls, value: list[UUID]) -> list[UUID]:
        """同一份文档在同一批里重复出现只建一个任务，避免浪费额度。"""
        seen: set[UUID] = set()
        unique: list[UUID] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique


class TaskOut(BaseModel):
    """任务摘要。用于列表与进度推送。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    document_filename: str | None = Field(default=None, description="来源文档的文件名，列表展示用")
    template_key: str
    template_name: str
    batch_id: UUID | None

    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    stage_message: str | None = Field(default=None, description="当前阶段的中文说明")

    error_code: str | None = None
    error_message: str | None = None

    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = Field(
        default=0, description="思考 token，已包含在 completion_tokens 内"
    )
    cached_tokens: int = Field(default=0, description="命中前缀缓存的输入 token")
    cost_usd: float = 0.0
    latency_ms: int | None = None
    attempts: int = 0

    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    @property
    def cost_cny(self) -> float:
        return usd_to_cny(self.cost_usd)

    @classmethod
    def from_row(cls, task, document_filename: str | None = None) -> TaskOut:
        payload = cls.model_validate(task)
        payload.document_filename = document_filename
        return payload


class TaskDeleteResponse(BaseModel):
    deleted: bool


class TaskWarningOut(BaseModel):
    """抽取过程中的一条非致命提示。"""

    path: str = Field(description="出问题的字段路径，如 party_a 或 items[0].name")
    kind: str = Field(description="问题类型：missing_required / type_coerced / ...")
    message: str


class ExtractionResultOut(BaseModel):
    """抽取结果。"""

    task_id: UUID
    data: dict[str, Any] = Field(description="校验并规范化后的结构化数据")
    raw_output: str = Field(description="模型原始返回，用于排查结果可疑的情况")
    warnings: list[TaskWarningOut] = Field(default_factory=list, description="非致命问题清单")
    truncated: bool = Field(description="输入文档是否因超长被截断")
    created_at: datetime

    @classmethod
    def from_row(cls, result) -> ExtractionResultOut:
        return cls(
            task_id=result.task_id,
            data=result.data,
            raw_output=result.raw_output,
            warnings=[TaskWarningOut(**item) for item in (result.warnings or [])],
            truncated=result.truncated,
            created_at=result.created_at,
        )


class TaskCreateResponse(BaseModel):
    batch_id: UUID
    tasks: list[TaskOut]
