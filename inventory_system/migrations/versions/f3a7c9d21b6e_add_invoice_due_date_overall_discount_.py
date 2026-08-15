"""add invoice due_date, overall_discount_amount, other_charges; add
DIGITAL_WALLET to payment_method

Revision ID: f3a7c9d21b6e
Revises: 65a248c62115
Create Date: 2026-08-16 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a7c9d21b6e'
down_revision: Union[str, None] = '65a248c62115'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New, optional invoice fields for the "New Bill" checkout screen —
    # nullable/zero-defaulted so every existing invoice row is unaffected
    # and every existing caller of generate_invoice (which doesn't pass
    # these) keeps producing exactly the totals it did before.
    op.add_column('invoices', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('invoices', sa.Column(
        'overall_discount_amount', sa.Numeric(precision=12, scale=2),
        server_default='0', nullable=False))
    op.add_column('invoices', sa.Column(
        'other_charges', sa.Numeric(precision=12, scale=2),
        server_default='0', nullable=False))
    op.create_check_constraint(
        'ck_invoices_overall_discount_amount_non_negative', 'invoices',
        'overall_discount_amount >= 0')
    op.create_check_constraint(
        'ck_invoices_other_charges_non_negative', 'invoices', 'other_charges >= 0')

    # ADD VALUE only registers the new label — never used in this same
    # transaction, so this is safe on Postgres 12+ (the minimum this
    # project already assumes elsewhere).
    op.execute("ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'DIGITAL_WALLET'")


def downgrade() -> None:
    op.drop_constraint('ck_invoices_other_charges_non_negative', 'invoices', type_='check')
    op.drop_constraint('ck_invoices_overall_discount_amount_non_negative', 'invoices',
                       type_='check')
    op.drop_column('invoices', 'other_charges')
    op.drop_column('invoices', 'overall_discount_amount')
    op.drop_column('invoices', 'due_date')
    # Postgres has no "DROP VALUE" for a native enum — removing a label
    # requires rebuilding the type and every dependent column, which isn't
    # worth doing for a downgrade path. An unused 'DIGITAL_WALLET' label
    # left in payment_method after downgrading is harmless (same tradeoff
    # already accepted by c08912894e62's downgrade for permission codes).
