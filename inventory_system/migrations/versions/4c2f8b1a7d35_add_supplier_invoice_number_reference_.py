"""add supplier_invoice_number, reference_number to purchase_orders;
created_at list indexes on purchase_orders and sales_orders

Revision ID: 4c2f8b1a7d35
Revises: 210ebd0168e0
Create Date: 2026-08-23 10:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4c2f8b1a7d35'
down_revision: Union[str, None] = '210ebd0168e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # supplier_invoice_number: the number printed on the *supplier's* bill,
    # entered by hand. Distinct from order_number, which this system
    # generates from PurchaseOrderSequence. A purchase register needs the
    # supplier's own number — that's what a tax authority reconciles
    # against — so it gets a real column rather than a custom_fields key.
    op.add_column('purchase_orders', sa.Column(
        'supplier_invoice_number', sa.String(length=120), nullable=True))

    # reference_number: free external reference, mirroring
    # sales_orders.reference_number added in 210ebd0168e0.
    op.add_column('purchase_orders', sa.Column(
        'reference_number', sa.String(length=120), nullable=True))

    # One supplier can't bill the same invoice number twice — catching a
    # double-entered bill at the database is the point. Scoped per
    # (organization, supplier) rather than per organization alone: two
    # different suppliers legitimately issue invoice "001". Partial, so any
    # number of rows may leave it blank (same shape as
    # ix_sales_orders_org_reference_number).
    op.create_index(
        'ix_purchase_orders_org_supplier_invoice_number', 'purchase_orders',
        ['organization_id', 'supplier_id', 'supplier_invoice_number'], unique=True,
        postgresql_where=sa.text('supplier_invoice_number IS NOT NULL'))

    # The transaction-list pages filter by date range and sort by
    # created_at within one organization — without these the list degrades
    # to a full scan of the org's orders as history grows.
    op.create_index('ix_purchase_orders_org_created_at', 'purchase_orders',
                    ['organization_id', 'created_at'])
    op.create_index('ix_sales_orders_org_created_at', 'sales_orders',
                    ['organization_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_sales_orders_org_created_at', table_name='sales_orders')
    op.drop_index('ix_purchase_orders_org_created_at', table_name='purchase_orders')
    op.drop_index('ix_purchase_orders_org_supplier_invoice_number',
                  table_name='purchase_orders')
    op.drop_column('purchase_orders', 'reference_number')
    op.drop_column('purchase_orders', 'supplier_invoice_number')
