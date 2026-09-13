"""create initial schema

创建四张核心表：
- documents          上传的文件与提取出的纯文本
- extraction_tasks   抽取任务（状态机 + 用量与成本统计）
- extraction_results 结构化抽取结果与模型原始输出
- custom_templates   用户自定义 JSON Schema 模板

Revision ID: 1a027b56e714
Revises:
Create Date: 2026-09-13 05:02:46.492939+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1a027b56e714"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 这两个枚举类型由 create_table 内的 sa.Enum(...) 隐式创建（PostgreSQL 原生枚举）。
# 但 DROP TABLE 不会连带删除类型定义，因此 downgrade 里必须显式 DROP TYPE，
# 否则「回退再升级」会在 CREATE TYPE 处报 type "xxx" already exists。
PG_ENUM_TYPES = ("task_status", "text_status")


def upgrade() -> None:
    """创建初始表结构。"""
    op.create_table(
        "custom_templates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prompt_hint", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_custom_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_custom_templates_name")),
    )
    op.create_index(
        "ix_custom_templates_created_at", "custom_templates", ["created_at"], unique=False
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=128), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("text_preview", sa.String(length=500), nullable=True),
        sa.Column(
            "text_status",
            sa.Enum(
                "extracted",
                "no_text_layer",
                "failed",
                name="text_status",
            ),
            server_default="extracted",
            nullable=False,
        ),
        sa.Column("text_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_documents_size_bytes_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("sha256", name=op.f("uq_documents_sha256")),
        sa.UniqueConstraint("stored_name", name=op.f("uq_documents_stored_name")),
    )
    op.create_index("ix_documents_created_at", "documents", ["created_at"], unique=False)

    op.create_table(
        "extraction_tasks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("template_key", sa.String(length=64), nullable=False),
        sa.Column("template_name", sa.String(length=128), nullable=False),
        sa.Column("custom_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "parsing",
                "extracting",
                "validating",
                "completed",
                "failed",
                name="task_status",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stage_message", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100", name=op.f("ck_extraction_tasks_progress_range")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_extraction_tasks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_tasks")),
    )
    op.create_index(
        "ix_extraction_tasks_batch_id", "extraction_tasks", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_extraction_tasks_status_created_at",
        "extraction_tasks",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_tasks_template_key", "extraction_tasks", ["template_key"], unique=False
    )

    op.create_table(
        "extraction_results",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["extraction_tasks.id"],
            name=op.f("fk_extraction_results_task_id_extraction_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_results")),
        sa.UniqueConstraint("task_id", name=op.f("uq_extraction_results_task_id")),
    )


def downgrade() -> None:
    """回退初始表结构，并清理枚举类型。"""
    op.drop_table("extraction_results")

    op.drop_index("ix_extraction_tasks_template_key", table_name="extraction_tasks")
    op.drop_index("ix_extraction_tasks_status_created_at", table_name="extraction_tasks")
    op.drop_index("ix_extraction_tasks_batch_id", table_name="extraction_tasks")
    op.drop_table("extraction_tasks")

    op.drop_index("ix_documents_created_at", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_custom_templates_created_at", table_name="custom_templates")
    op.drop_table("custom_templates")

    # DROP TABLE 不会删除 PostgreSQL 原生枚举类型，必须显式清理，
    # 否则再次 upgrade 时 CREATE TYPE 会失败。
    bind = op.get_bind()
    for enum_name in PG_ENUM_TYPES:
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)
