"""Initial schema — все таблицы проекта.

Revision ID: 001_initial
Revises: None
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("telegram_id", sa.BigInteger, unique=True, nullable=True),
        sa.Column("telegram_username", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("username", sa.String(64), unique=True, nullable=True),
        sa.Column("password_hash", sa.String(128), nullable=True),
        sa.Column("sub_token", sa.String(36), unique=True, nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_sub_token", "users", ["sub_token"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("currency", sa.String(8), nullable=False, server_default="RUB"),
        sa.Column("status", sa.String(16), nullable=False, server_default="paid"),
        sa.Column("period_start", sa.DateTime(), nullable=True),
        sa.Column("period_end", sa.DateTime(), nullable=True),
        sa.Column("note", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_payments_id", "payments", ["id"])

    op.create_table(
        "vpn_keys",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False, server_default="default"),
        sa.Column("uuid", sa.String(36), unique=True, nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False, server_default="vless"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("data_limit", sa.BigInteger(), nullable=True),
        sa.Column("data_used", sa.BigInteger(), nullable=True, server_default=sa.text("0")),
        sa.Column("expire_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_vpn_keys_id", "vpn_keys", ["id"])
    op.create_index("ix_vpn_keys_uuid", "vpn_keys", ["uuid"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_audit_log_id", "audit_log", ["id"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "invite_keys",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(32), unique=True, nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_used", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_invite_keys_id", "invite_keys", ["id"])
    op.create_index("ix_invite_keys_key", "invite_keys", ["key"])


def downgrade() -> None:
    op.drop_table("invite_keys")
    op.drop_table("app_settings")
    op.drop_table("audit_log")
    op.drop_table("vpn_keys")
    op.drop_table("payments")
    op.drop_table("users")
