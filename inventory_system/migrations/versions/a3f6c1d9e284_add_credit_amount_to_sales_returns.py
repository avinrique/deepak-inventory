"""add credit_amount to sales_returns

Revision ID: a3f6c1d9e284
Revises: f3a7c9d21b6e
Create Date: 2026-08-20 07:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f6c1d9e284'
down_revision: Union[str, None] = 'f3a7c9d21b6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A sales return previously restored inventory but recorded no
    # monetary value at all, so a customer's outstanding balance never
    # reflected goods they returned. server_default='0' backfills every
    # existing row (their credit is unrecoverable after the fact — this
    # only fixes returns recorded from here on), then the default is
    # dropped so future inserts must supply a real computed value, same
    # two-step pattern 205f1c3347a5 used for must_change_password.
    op.add_column('sales_returns', sa.Column(
        'credit_amount', sa.Numeric(precision=12, scale=2),
        server_default='0', nullable=False))
    op.alter_column('sales_returns', 'credit_amount', server_default=None)


def downgrade() -> None:
    op.drop_column('sales_returns', 'credit_amount')
