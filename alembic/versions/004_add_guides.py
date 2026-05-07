"""Add guides table for connection instructions.

Revision ID: 004_add_guides
Revises: 003_add_user_is_free
Create Date: 2025-01-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_add_guides"
down_revision: Union[str, None] = "003_add_user_is_free"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "guides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("app_name", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False, server_default="all"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_guides_id", "guides", ["id"])
    op.create_index("ix_guides_slug", "guides", ["slug"])
    op.create_index("ix_guides_is_active", "guides", ["is_active"])


def downgrade() -> None:
    op.drop_table("guides")
