"""restrict organization_id cascade on business/financial tables

Revision ID: b7d4e91a5c33
Revises: a3f6c1d9e284
Create Date: 2026-08-20 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7d4e91a5c33'
down_revision: Union[str, None] = 'a3f6c1d9e284'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every business/financial/inventory table's organization_id FK was
# CASCADE, unlike customer_id/supplier_id/product_id/warehouse_id FKs on
# these same tables (or ON THOSE entities' own children), which correctly
# RESTRICT — see e.g. Invoice.sales_order_id, Payment.invoice_id. There is
# no delete_organization feature anywhere in the app today (verified: no
# such method exists in organization_repository.py/organization_service.py)
# so this was never reachable through normal use, but a single
# ``DELETE FROM organizations WHERE id=...`` run outside the app (a support
# script, a bad migration, a future "offboard a tenant" feature added
# without noticing this) would silently cascade-delete every invoice,
# payment, purchase order, and inventory-ledger entry for that
# organization — exactly what "financial/history records must not be
# silently deleted" forbids. audit_logs/database_backups intentionally stay
# SET NULL (the trail is meant to outlive the org, see AuditLog's
# docstring); user_organizations/invoice_sequences/purchase_order_sequences
# stay CASCADE (membership rows and pure numbering counters, not history).
_RESTRICT_TABLES = [
    "brands", "categories", "units", "products", "warehouses", "inventory",
    "inventory_transactions", "stock_adjustments", "stock_transfers", "suppliers",
    "purchase_orders", "goods_receipts", "purchase_returns", "customers",
    "sales_orders", "invoices", "payments", "sales_returns",
]


def upgrade() -> None:
    for table in _RESTRICT_TABLES:
        constraint = f"{table}_organization_id_fkey"
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, "organizations",
                              ["organization_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    for table in _RESTRICT_TABLES:
        constraint = f"{table}_organization_id_fkey"
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, "organizations",
                              ["organization_id"], ["id"], ondelete="CASCADE")
