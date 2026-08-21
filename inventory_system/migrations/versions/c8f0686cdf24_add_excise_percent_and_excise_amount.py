"""add excise_percent to products and sales_order_items, excise_amount to
invoices

Revision ID: c8f0686cdf24
Revises: aa93d2683eaa
Create Date: 2026-08-21 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f0686cdf24'
down_revision: Union[str, None] = 'aa93d2683eaa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Excise duty — a government levy distinct from sales tax (tax_percent),
    # computed on the same post-discount base independently of tax (see
    # app.domain.sales.line_excise_after_discount). Zero-defaulted so every
    # existing product/line/invoice is unaffected.
    op.add_column('products', sa.Column(
        'excise_percent', sa.Numeric(precision=5, scale=2),
        server_default='0', nullable=False))
    op.create_check_constraint(
        'ck_products_excise_percent_range', 'products',
        'excise_percent >= 0 AND excise_percent <= 100')

    op.add_column('sales_order_items', sa.Column(
        'excise_percent', sa.Numeric(precision=5, scale=2),
        server_default='0', nullable=False))
    op.create_check_constraint(
        'ck_so_items_excise_percent_range', 'sales_order_items',
        'excise_percent >= 0 AND excise_percent <= 100')

    op.add_column('invoices', sa.Column(
        'excise_amount', sa.Numeric(precision=12, scale=2),
        server_default='0', nullable=False))
    op.create_check_constraint(
        'ck_invoices_excise_amount_non_negative', 'invoices', 'excise_amount >= 0')


def downgrade() -> None:
    op.drop_constraint('ck_invoices_excise_amount_non_negative', 'invoices', type_='check')
    op.drop_column('invoices', 'excise_amount')

    op.drop_constraint('ck_so_items_excise_percent_range', 'sales_order_items', type_='check')
    op.drop_column('sales_order_items', 'excise_percent')

    op.drop_constraint('ck_products_excise_percent_range', 'products', type_='check')
    op.drop_column('products', 'excise_percent')
