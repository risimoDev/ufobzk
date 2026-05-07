"""Add servers table for remote Xray nodes.

Revision ID: 002_add_servers
Revises: 001_initial
Create Date: 2025-01-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_servers"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "servers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("api_url", sa.String(255), nullable=False),
        sa.Column("api_token", sa.String(128), nullable=False),
        sa.Column("region", sa.String(16), nullable=False, server_default="eu"),
        sa.Column("role", sa.String(16), nullable=False, server_default="proxy"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("reality_public_key", sa.String(128), nullable=True),
        sa.Column("reality_private_key", sa.String(128), nullable=True),
        sa.Column("reality_dest", sa.String(128), nullable=True),
        sa.Column("reality_server_names", sa.String(255), nullable=True),
        sa.Column("reality_short_id", sa.String(32), nullable=True),
        sa.Column("ws_port", sa.Integer, nullable=False, server_default="443"),
        sa.Column("reality_port", sa.Integer, nullable=False, server_default="2053"),
        sa.Column("grpc_port", sa.Integer, nullable=False, server_default="8445"),
        sa.Column("xhttp_port", sa.Integer, nullable=False, server_default="8444"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync", sa.DateTime(), nullable=True),
        sa.Column("last_sync_status", sa.String(16), nullable=True),
    )
    op.create_index("ix_servers_id", "servers", ["id"])
    op.create_index("ix_servers_region", "servers", ["region"])
    op.create_index("ix_servers_is_active", "servers", ["is_active"])


def downgrade() -> None:
    op.drop_table("servers")
