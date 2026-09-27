"""add task console fields

Revision ID: b07d41c3e9a2
Revises: a1c4e8f7b2d9
Create Date: 2026-09-27 20:10:00.000000

任务控制台扩展列：name / source / ref_kind / ref_id / ref_label——
对话/文档中心/任务拆解/定时任务等来源入台后统一按此口径检索与展示。
SQLite 加列走 batch_alter_table（自带后端差异处理）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b07d41c3e9a2"
down_revision: str | Sequence[str] | None = "a1c4e8f7b2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("name", sa.String(length=200), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("source", sa.String(length=24), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("ref_kind", sa.String(length=24), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("ref_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("ref_label", sa.String(length=200), nullable=False, server_default="")
        )
        batch_op.create_index(op.f("ix_tasks_source"), ["source"], unique=False)
        batch_op.create_index(op.f("ix_tasks_ref_id"), ["ref_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index(op.f("ix_tasks_source"), table_name="tasks")
        batch_op.drop_index(op.f("ix_tasks_ref_id"), table_name="tasks")
        batch_op.drop_column("name")
        batch_op.drop_column("source")
        batch_op.drop_column("ref_kind")
        batch_op.drop_column("ref_id")
        batch_op.drop_column("ref_label")
