"""Add sub_token_updated_at to users

Revision ID: 006
Revises: 005
Create Date: 2026-05-24
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("users")]
    if "sub_token_updated_at" not in columns:
        op.add_column("users", sa.Column("sub_token_updated_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    op.drop_column("users", "sub_token_updated_at")
