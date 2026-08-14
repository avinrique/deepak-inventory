"""Canonical permission/role catalog — the single source of truth seeded by
scripts/init_db.py and enforced by app.security.authorization. Adding a new
protected operation means adding its permission code here, nowhere else.

Role design notes:
- OWNER and ADMIN currently get an identical, full permission set. They're
  kept as distinct roles because the *business* distinction between them
  (an Owner can't be removed/demoted, there's exactly one per organization)
  is an invariant enforced elsewhere, not a permission-set difference.
- MANAGER has every operational permission (product/inventory/sales/
  purchase/reports) but not users.manage/settings.manage — a store manager
  runs operations, not the account itself.
- SALES_STAFF and PURCHASE_STAFF are deliberately narrower than MANAGER:
  front-line staff can create and read, but cancelling a sale, refunding a
  sale, or approving a purchase requires MANAGER/ACCOUNTANT — a common
  real-world separation-of-duties control.
- ACCOUNTANT is read-heavy plus the two "money already moved" operations
  (sales.refund, purchase.approve) that finance typically signs off on.
- VIEWER is read-only across every domain.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    description: str


PERMISSIONS: list[PermissionDef] = [
    PermissionDef("product.create", "Create products"),
    PermissionDef("product.read", "View products"),
    PermissionDef("product.update", "Update products"),
    PermissionDef("product.delete", "Delete products"),
    PermissionDef("inventory.read", "View stock levels"),
    PermissionDef("inventory.adjust", "Adjust stock quantities"),
    PermissionDef("inventory.transfer", "Transfer stock between locations"),
    PermissionDef("sales.create", "Create a sale"),
    PermissionDef("sales.read", "View sales"),
    PermissionDef("sales.cancel", "Cancel a sale"),
    PermissionDef("sales.refund", "Refund a sale"),
    PermissionDef("purchase.create", "Create a purchase"),
    PermissionDef("purchase.read", "View purchases"),
    PermissionDef("purchase.approve", "Approve a purchase"),
    PermissionDef("reports.view", "View reports"),
    PermissionDef("users.manage",
                  "Create/activate/deactivate users, reset passwords, assign roles"),
    PermissionDef("settings.manage", "Manage organization settings"),
    PermissionDef("audit_logs.view", "View the audit trail"),
]
PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)

_ALL = sorted(PERMISSION_CODES)
_PRODUCT_READ_ONLY = ["product.read", "inventory.read"]
_OPERATIONAL = [
    "product.create", "product.read", "product.update", "product.delete",
    "inventory.read", "inventory.adjust", "inventory.transfer",
    "sales.create", "sales.read", "sales.cancel", "sales.refund",
    "purchase.create", "purchase.read", "purchase.approve",
    "reports.view",
]

# Role name -> permission codes granted by default. Seeded by
# scripts/init_db.py. Changing this after go-live affects only newly-seeded
# installs — an existing database's grants must be migrated deliberately,
# not silently redefined by editing this dict.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "OWNER": _ALL,
    "ADMIN": _ALL,
    "MANAGER": _OPERATIONAL,
    "INVENTORY_MANAGER": [
        "product.create", "product.read", "product.update", "product.delete",
        "inventory.read", "inventory.adjust", "inventory.transfer",
        "reports.view",
    ],
    "SALES_STAFF": [*_PRODUCT_READ_ONLY, "sales.create", "sales.read"],
    "PURCHASE_STAFF": [*_PRODUCT_READ_ONLY, "purchase.create", "purchase.read"],
    "ACCOUNTANT": [
        "product.read", "inventory.read",
        "sales.read", "sales.refund",
        "purchase.read", "purchase.approve",
        "reports.view", "audit_logs.view",
    ],
    "VIEWER": ["product.read", "inventory.read", "sales.read", "purchase.read",
              "reports.view"],
}
ROLE_NAMES: tuple[str, ...] = tuple(ROLE_PERMISSIONS)
