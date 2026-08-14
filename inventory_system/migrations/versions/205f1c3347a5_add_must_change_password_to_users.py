"""add must_change_password to users

Revision ID: 205f1c3347a5
Revises: 091eeb36e646
Create Date: 2026-08-13 20:22:33.830243

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '205f1c3347a5'
down_revision: Union[str, None] = '091eeb36e646'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate produced `nullable=False` with no default, which fails
    # against a users table that already has rows (adjusted by hand): add
    # it with a server-side default so existing rows backfill to False,
    # then drop the server default so the column matches the model (which
    # only has a client-side default, applied on INSERT going forward).
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(),
                                     nullable=False, server_default=sa.text('false')))
    op.alter_column('users', 'must_change_password', server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'must_change_password')
