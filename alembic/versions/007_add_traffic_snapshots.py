"""Add traffic_snapshots table

Revision ID: 007
Revises: 006
Create Date: 2026-05-24
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "traffic_snapshots" not in inspector.get_table_names():
        op.create_table(
            "traffic_snapshots",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "vpn_key_id",
                sa.Integer,
                sa.ForeignKey("vpn_keys.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("bytes_delta", sa.BigInteger, nullable=False, server_default="0"),
            sa.Column("total_bytes", sa.BigInteger, nullable=False, server_default="0"),
            sa.Column(
                "recorded_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "ix_traffic_snapshots_vpn_key_id",
            "traffic_snapshots",
            ["vpn_key_id"],
        )
        op.create_index(
            "ix_traffic_snapshots_recorded_at",
            "traffic_snapshots",
            ["recorded_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_traffic_snapshots_recorded_at", table_name="traffic_snapshots")
    op.drop_index("ix_traffic_snapshots_vpn_key_id", table_name="traffic_snapshots")
    op.drop_table("traffic_snapshots")
