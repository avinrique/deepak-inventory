"""SQLAlchemy 2.x declarative ORM models.

Importing this package registers every model on Base.metadata — the one
thing Alembic autogenerate and Base.metadata.create_all() both need.
Individual modules should still be imported explicitly wherever a specific
class is used; this file exists for its import side effect.
"""
from app.models.audit_log import AuditLog
from app.models.backup import DatabaseBackup
from app.models.base import Base
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.inventory import Inventory, InventoryTransaction, StockAdjustment, StockTransfer
from app.models.organization import Organization, UserOrganization
from app.models.product import Product
from app.models.purchase_order import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderSequence,
    PurchaseReturn,
)
from app.models.role import Permission, Role, RolePermission
from app.models.sales_order import (
    Invoice,
    InvoiceSequence,
    Payment,
    SalesOrder,
    SalesOrderItem,
    SalesReturn,
)
from app.models.supplier import Supplier
from app.models.unit import Unit
from app.models.user import User
from app.models.warehouse import Warehouse

__all__ = [
    "Base",
    "User",
    "Role",
    "Permission",
    "RolePermission",
    "Organization",
    "UserOrganization",
    "AuditLog",
    "DatabaseBackup",
    "Category",
    "Brand",
    "Unit",
    "Product",
    "Warehouse",
    "Inventory",
    "InventoryTransaction",
    "StockAdjustment",
    "StockTransfer",
    "Supplier",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "GoodsReceipt",
    "GoodsReceiptItem",
    "PurchaseOrderSequence",
    "PurchaseReturn",
    "Customer",
    "SalesOrder",
    "SalesOrderItem",
    "Invoice",
    "Payment",
    "SalesReturn",
    "InvoiceSequence",
]
