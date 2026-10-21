"""tool policies fine grained permission

Revision ID: 380aba23ca98
Revises: b07d41c3e9a2
Create Date: 2026-10-21 13:54:46.008563

细粒度权限策略表：角色 × 工具的启停（PRD §2.11「工具细粒度权限管控，可对不同角色启用/禁用工具」）。
唯一键 (tenant, role, tool_name)；enabled=0 即该角色禁用该工具，无行 = 默认放行。
链路：api/admin /admin/tool-policies → services/tool_policy → 本表；
      执行面（工具清单 / invoke / Agent 步骤 / 审批放行复核）四处读同一判定源。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "380aba23ca98"
down_revision: str | Sequence[str] | None = "b07d41c3e9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tool_policies",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant", "role", "tool_name", name="uq_tool_policy_tenant_role_tool"),
    )
    op.create_index(op.f("ix_tool_policies_role"), "tool_policies", ["role"], unique=False)
    op.create_index(op.f("ix_tool_policies_tenant"), "tool_policies", ["tenant"], unique=False)
    op.create_index(
        op.f("ix_tool_policies_tool_name"), "tool_policies", ["tool_name"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_tool_policies_tool_name"), table_name="tool_policies")
    op.drop_index(op.f("ix_tool_policies_tenant"), table_name="tool_policies")
    op.drop_index(op.f("ix_tool_policies_role"), table_name="tool_policies")
    op.drop_table("tool_policies")
