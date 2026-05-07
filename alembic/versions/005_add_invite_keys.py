"""Add invite_keys table

Revision ID: 005
Revises: 004_add_guides
Create Date: 2026-05-08
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004_add_guides'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'invite_keys' not in inspector.get_table_names():
        op.create_table(
            'invite_keys',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('key', sa.String(length=64), nullable=False),
            sa.Column('is_used', sa.Boolean(), nullable=False, server_default='0'),
            sa.Column('used_by', sa.Integer(), nullable=True),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('key'),
            sa.ForeignKeyConstraint(['used_by'], ['users.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        )
        op.create_index(op.f('ix_invite_keys_id'), 'invite_keys', ['id'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'invite_keys' in inspector.get_table_names():
        op.drop_index(op.f('ix_invite_keys_id'), table_name='invite_keys')
        op.drop_table('invite_keys')
