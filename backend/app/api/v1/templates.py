"""抽取模板接口。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.schemas.template import (
    CustomTemplateCreate,
    CustomTemplateOut,
    TemplateDeleteResponse,
    TemplateOut,
)
from app.services import template_service

logger = get_logger(__name__)

router = APIRouter(prefix="/templates", tags=["模板"])


@router.get(
    "",
    response_model=list[TemplateOut],
    summary="模板列表",
    description=(
        "返回全部可用模板：3 套内置模板（合同 / 简历 / 发票）在前，"
        "用户自定义模板在后。\n\n"
        "每个字段都带中文 `label`，前端据此渲染结果卡片——"
        "用户看到的是「甲方」而不是 `party_a`。"
    ),
)
async def list_templates(db: DbSession) -> list[TemplateOut]:
    templates = await template_service.list_all_templates(db)
    custom_rows = {str(row.id): row for row in await template_service.list_custom_templates(db)}

    return [
        TemplateOut.from_template(
            template,
            created_at=custom_rows[template.template_id].created_at
            if template.template_id and template.template_id in custom_rows
            else None,
        )
        for template in templates
    ]


@router.post(
    "",
    response_model=CustomTemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建自定义模板",
    description=(
        "传入标准 JSON Schema（Draft 2020-12），根节点必须是 object。\n\n"
        "Schema 在入库前就会做完整性与合法性校验——"
        "**不能让一个语法错误的 schema 存进数据库**，否则用户要到建任务时才发现，"
        "而且报错点离问题源头很远。"
    ),
)
async def create_custom_template(db: DbSession, payload: CustomTemplateCreate) -> CustomTemplateOut:
    row = await template_service.create_custom_template(
        db,
        name=payload.name,
        schema=payload.json_schema,
        description=payload.description,
        prompt_hint=payload.prompt_hint,
    )
    return CustomTemplateOut.model_validate(row)


@router.delete(
    "/{template_id}",
    response_model=TemplateDeleteResponse,
    summary="删除自定义模板",
    description=(
        "内置模板不可删除。\n\n"
        "历史任务存的是模板快照（名称 + schema），因此删除模板**不影响**"
        "历史任务的展示与重跑。但有任务正在使用该模板时会拒绝删除。"
    ),
)
async def delete_custom_template(db: DbSession, template_id: UUID) -> TemplateDeleteResponse:
    await template_service.delete_custom_template(db, template_id)
    return TemplateDeleteResponse(deleted=True)
