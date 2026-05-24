"""Add notes and speed_limit_kbps to vpn_keys

Revision ID: 008
Revises: 007
Create Date: 2026-05-24
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '008'
down_revision: Union[str, None] = '007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("vpn_keys")}
    if "notes" not in existing_cols:
        op.add_column("vpn_keys", sa.Column("notes", sa.Text, nullable=True))
    if "speed_limit_kbps" not in existing_cols:
        op.add_column("vpn_keys", sa.Column("speed_limit_kbps", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("vpn_keys", "speed_limit_kbps")
    op.drop_column("vpn_keys", "notes")
