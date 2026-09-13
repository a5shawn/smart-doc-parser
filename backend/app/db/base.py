"""SQLAlchemy 声明式基类。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一的约束命名规则。
# 不配这个的话，PostgreSQL 会给约束取随机名字（如 documents_sha256_key），
# 导致 Alembic 自动生成的迁移里出现不可读的名字，且后续想 DROP 时无从下手。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """创建/更新时间。

    全部使用带时区的 ``TIMESTAMPTZ``，由数据库生成时间戳（``now()``），
    避免应用服务器时钟不一致导致排序错乱。
    Python 侧一律使用 ``datetime.now(UTC)``，**不要用已废弃的 ``datetime.utcnow()``**
    （它返回 naive 对象，写入 TIMESTAMPTZ 时会按本地时区解释，凭空差 8 小时）。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
