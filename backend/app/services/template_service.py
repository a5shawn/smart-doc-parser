"""模板服务：内置模板 + 用户自定义 schema。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schema_utils import normalize_schema
from app.ai.templates import (
    ExtractionTemplate,
    builtin_template_keys,
    get_builtin_template,
    load_builtin_templates,
)
from app.core.errors import (
    ConflictError,
    InvalidSchemaError,
    TemplateInUseError,
    TemplateNotFoundError,
)
from app.core.logging import get_logger
from app.db.models import CustomTemplate, ExtractionTask

logger = get_logger(__name__)

#: 自定义模板 key 的前缀。用它把自定义模板与内置模板区分开，
#: 任务表的 template_key 一个字段就能同时引用两者。
CUSTOM_PREFIX = "custom:"


def custom_key(template_id: UUID) -> str:
    return f"{CUSTOM_PREFIX}{template_id}"


def is_custom_key(key: str) -> bool:
    return key.startswith(CUSTOM_PREFIX)


def _template_from_row(row: CustomTemplate) -> ExtractionTemplate:
    return ExtractionTemplate.from_custom_schema(
        key=custom_key(row.id),
        name=row.name,
        schema=row.schema,
        description=row.description or "",
        prompt_hint=row.prompt_hint or "",
        template_id=str(row.id),
    )


async def list_custom_templates(db: AsyncSession) -> Sequence[CustomTemplate]:
    result = await db.execute(select(CustomTemplate).order_by(CustomTemplate.created_at.desc()))
    return result.scalars().all()


async def list_all_templates(db: AsyncSession) -> list[ExtractionTemplate]:
    """内置模板在前，自定义模板在后。"""
    templates = list(load_builtin_templates().values())
    templates.extend(_template_from_row(row) for row in await list_custom_templates(db))
    return templates


async def get_template(db: AsyncSession, key: str) -> ExtractionTemplate:
    """按 key 取模板。key 可以是内置模板名或 ``custom:<uuid>``。"""
    if not is_custom_key(key):
        return get_builtin_template(key)

    raw_id = key.removeprefix(CUSTOM_PREFIX)
    try:
        template_id = UUID(raw_id)
    except ValueError as exc:
        raise TemplateNotFoundError(
            f"模板标识不合法：{key}", details={"template_key": key}
        ) from exc

    row = await db.get(CustomTemplate, template_id)
    if row is None:
        raise TemplateNotFoundError(
            f"自定义模板不存在：{key}",
            details={"template_key": key, "available": sorted(builtin_template_keys())},
        )
    return _template_from_row(row)


async def create_custom_template(
    db: AsyncSession,
    *,
    name: str,
    schema: dict,
    description: str | None = None,
    prompt_hint: str | None = None,
) -> CustomTemplate:
    """新建自定义模板。

    schema 在这里就做规范化与合法性检查——**不能让一个语法错误的 schema
    存进数据库**，否则用户要到建任务时才发现，而且报错点离问题源头很远。
    """
    normalized = normalize_schema(schema)

    row = CustomTemplate(
        name=name.strip(),
        description=(description or "").strip() or None,
        schema=normalized,
        prompt_hint=(prompt_hint or "").strip() or None,
    )
    db.add(row)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"模板名称「{name}」已存在，请换一个名字",
            details={"name": name},
        ) from exc

    await db.refresh(row)
    logger.info(
        "custom_template_created",
        template_id=str(row.id),
        name=row.name,
        field_count=len(normalized.get("properties", {})),
    )
    return row


async def delete_custom_template(db: AsyncSession, template_id: UUID) -> None:
    """删除自定义模板。

    任务表里存的是模板快照（名称 + schema），因此删除模板**不会**影响历史任务
    的展示与复现。但正在使用它的任务仍应阻止删除，避免用户困惑。
    """
    row = await db.get(CustomTemplate, template_id)
    if row is None:
        raise TemplateNotFoundError(details={"template_id": str(template_id)})

    in_use = await db.scalar(
        select(ExtractionTask.id)
        .where(ExtractionTask.template_key == custom_key(template_id))
        .limit(1)
    )
    if in_use is not None:
        raise TemplateInUseError(
            f"模板「{row.name}」已被抽取任务使用，无法删除。"
            "历史任务的展示不受影响，如需停用请直接不再选用它。",
            details={"template_id": str(template_id)},
        )

    await db.delete(row)
    await db.commit()
    logger.info("custom_template_deleted", template_id=str(template_id))


def validate_custom_schema(schema: dict) -> dict:
    """对外暴露的 schema 校验，供接口层在入库前快速失败。"""
    try:
        return normalize_schema(schema)
    except InvalidSchemaError:
        raise
    except Exception as exc:
        raise InvalidSchemaError(f"JSON Schema 不合法：{exc}") from exc
