"""add delivery_date, reference_number, custom_fields to sales_orders;
custom_fields to purchase_orders

Revision ID: 210ebd0168e0
Revises: c8f0686cdf24
Create Date: 2026-08-21 09:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '210ebd0168e0'
down_revision: Union[str, None] = 'c8f0686cdf24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # delivery_date: when goods are expected to physically reach the
    # customer — distinct from Invoice.due_date (payment due).
    op.add_column('sales_orders', sa.Column('delivery_date', sa.Date(), nullable=True))

    # reference_number: customer/external-supplied reference (e.g. their PO
    # number), separate from the system-generated sequential
    # Invoice.invoice_number. Unique per organization only when set.
    op.add_column('sales_orders', sa.Column(
        'reference_number', sa.String(length=120), nullable=True))
    op.create_index(
        'ix_sales_orders_org_reference_number', 'sales_orders',
        ['organization_id', 'reference_number'], unique=True,
        postgresql_where=sa.text('reference_number IS NOT NULL'))

    # custom_fields: ad-hoc key/value pairs, no field-type system or
    # org-level schema, needed on both sales orders (New Bill + Sales Order
    # form) and purchase orders.
    op.add_column('sales_orders', sa.Column(
        'custom_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('purchase_orders', sa.Column(
        'custom_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('purchase_orders', 'custom_fields')
    op.drop_column('sales_orders', 'custom_fields')
    op.drop_index('ix_sales_orders_org_reference_number', table_name='sales_orders')
    op.drop_column('sales_orders', 'reference_number')
    op.drop_column('sales_orders', 'delivery_date')
