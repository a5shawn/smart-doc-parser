"""抽取模板的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.ai.templates import ExtractionTemplate


class TemplateFieldOut(BaseModel):
    """模板中的一个字段。前端据此渲染结果卡片与表单标签。"""

    name: str
    label: str = Field(description="中文标签，前端用它替代字段名做展示")
    type: str
    description: str = ""
    required: bool = False
    enum: list[str] = Field(default_factory=list)
    item_type: str | None = None
    item_fields: list[TemplateFieldOut] = Field(default_factory=list)


class TemplateOut(BaseModel):
    key: str
    name: str
    description: str = ""
    doc_types: list[str] = Field(default_factory=list, description="适用的文档类型")
    prompt_hint: str = ""
    fields: list[TemplateFieldOut] = Field(default_factory=list)
    builtin: bool = Field(description="内置模板不可删除")
    template_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_template(
        cls, template: ExtractionTemplate, *, created_at: datetime | None = None
    ) -> TemplateOut:
        return cls(
            key=template.key,
            name=template.name,
            description=template.description,
            doc_types=list(template.doc_types),
            prompt_hint=template.prompt_hint,
            fields=[_field_out(field) for field in template.fields],
            builtin=template.builtin,
            template_id=UUID(template.template_id) if template.template_id else None,
            created_at=created_at,
        )


def _field_out(field) -> TemplateFieldOut:
    return TemplateFieldOut(
        name=field.name,
        label=field.label,
        type=field.type,
        description=field.description,
        required=field.required,
        enum=list(field.enum),
        item_type=field.item_type,
        item_fields=[_field_out(child) for child in field.item_fields],
    )


class CustomTemplateCreate(BaseModel):
    """新建自定义模板。

    字段名用 ``json_schema`` + 别名 ``schema``：``schema`` 是 Pydantic
    BaseModel 的保留名字，直接用作字段名会冲突，但对外的 JSON 又希望是
    ``schema`` 这个自然的名字，所以用别名并开启按名解析。
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=128, description="模板名称，需唯一")
    json_schema: dict[str, Any] = Field(
        alias="schema",
        description="标准 JSON Schema（Draft 2020-12），根节点必须是 object",
    )
    description: str | None = Field(default=None, max_length=500)
    prompt_hint: str | None = Field(
        default=None, max_length=1000, description="附加的业务规则，例如金额单位要求"
    )


class CustomTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    prompt_hint: str | None
    created_at: datetime


class TemplateDeleteResponse(BaseModel):
    deleted: bool
