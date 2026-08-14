"""Canonical permission/role catalog — the single source of truth seeded by
scripts/init_db.py and enforced by app.security.authorization. Adding a new
protected operation means adding its permission code here, nowhere else.

Renaming or splitting a code here (e.g. the old singular product.read /
purchase.read / users.manage codes) is a real schema-adjacent change, not
just an edit to this file — see migrations/versions for the migration that
renames existing `permissions.code` rows and re-points `role_permissions`
grants so this file staying in sync with the seeded catalog doesn't
silently orphan an existing install's grants. This dict is what
scripts/init_db.py additively seeds from (new codes get inserted, existing
ones are left alone) — it does not delete/rename rows, so a rename here
always needs a matching migration.

Role design notes:
- OWNER and ADMIN currently get an identical, full permission set. They're
  kept as distinct roles because the *business* distinction between them
  (an Owner can't be removed/demoted, there's exactly one per organization)
  is an invariant enforced in app.services.user_service (OwnerProtectedError),
  not a permission-set difference.
- MANAGER has every operational permission (products/inventory/sales/
  purchases/reports) but none of the users.* codes and not settings.manage
  — a store manager runs operations, not the account itself.
- SALES_STAFF and PURCHASE_STAFF are deliberately narrower than MANAGER:
  front-line staff can create and view, but cancelling a sale, refunding a
  sale, or approving a purchase requires MANAGER/ACCOUNTANT — a common
  real-world separation-of-duties control.
- ACCOUNTANT is read-heavy plus the two "money already moved" operations
  (sales.refund, purchases.approve) that finance typically signs off on.
- VIEWER is read-only across every domain.
- backup.create/backup.restore are granted only through _ALL (OWNER/ADMIN)
  — deliberately absent from _OPERATIONAL, so no other role gets them even
  though MANAGER otherwise has nearly every operational permission.
- users.* (view/create/update/deactivate/reset_password/manage_roles) are
  granular on purpose, replacing a single "users.manage" umbrella: a role
  can be given users.view without also being able to deactivate someone or
  reset their password. Only OWNER/ADMIN (_ALL) get the full set today;
  nothing in _OPERATIONAL grants any of them.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    description: str


PERMISSIONS: list[PermissionDef] = [
    PermissionDef("products.create", "Create products"),
    PermissionDef("products.view", "View products"),
    PermissionDef("products.update", "Update products"),
    PermissionDef("products.delete", "Delete products"),
    PermissionDef("warehouse.manage", "Create/update warehouses"),
    PermissionDef("inventory.view", "View stock levels"),
    PermissionDef("inventory.adjust", "Adjust stock quantities"),
    PermissionDef("inventory.transfer", "Transfer stock between locations"),
    PermissionDef("sales.create", "Create a sales order"),
    PermissionDef("sales.view", "View sales orders"),
    PermissionDef("sales.update", "Edit a draft sales order; manage customers"),
    PermissionDef("sales.confirm", "Confirm a sales order"),
    PermissionDef("sales.fulfill", "Fulfill a sales order (deduct inventory)"),
    PermissionDef("sales.cancel", "Cancel a sales order"),
    PermissionDef("sales.invoice", "Generate an invoice for a sales order"),
    PermissionDef("sales.payment", "Record a payment against an invoice"),
    PermissionDef("sales.refund", "Refund/return a sale"),
    PermissionDef("purchases.create", "Create a purchase order"),
    PermissionDef("purchases.view", "View purchase orders"),
    PermissionDef("purchases.update", "Edit a draft purchase order; manage suppliers"),
    PermissionDef("purchases.approve", "Approve a purchase order"),
    PermissionDef("purchases.receive", "Receive goods against a purchase order"),
    PermissionDef("purchases.cancel", "Cancel a purchase order"),
    PermissionDef("purchases.return", "Return received goods to a supplier"),
    PermissionDef("reports.view", "View reports"),
    PermissionDef("reports.export", "Export/print reports (CSV/Excel/PDF)"),
    PermissionDef("users.view", "View staff accounts and roles"),
    PermissionDef("users.create", "Create staff accounts"),
    PermissionDef("users.update", "Update/reactivate staff accounts"),
    PermissionDef("users.deactivate", "Deactivate staff accounts"),
    PermissionDef("users.reset_password", "Reset a staff account's password"),
    PermissionDef("users.manage_roles", "Assign roles to staff accounts"),
    PermissionDef("settings.view", "View organization settings"),
    PermissionDef("settings.manage", "Manage organization settings"),
    PermissionDef("audit_logs.view", "View the audit trail"),
    PermissionDef("backup.create", "Create a database backup"),
    PermissionDef("backup.restore", "Restore the database from a backup"),
]
PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)

_ALL = sorted(PERMISSION_CODES)
_PRODUCT_VIEW_ONLY = ["products.view", "inventory.view"]
_OPERATIONAL = [
    "products.create", "products.view", "products.update", "products.delete",
    "warehouse.manage", "inventory.view", "inventory.adjust", "inventory.transfer",
    "sales.create", "sales.view", "sales.update", "sales.confirm", "sales.fulfill",
    "sales.cancel", "sales.invoice", "sales.payment", "sales.refund",
    "purchases.create", "purchases.view", "purchases.update", "purchases.approve",
    "purchases.receive", "purchases.cancel", "purchases.return",
    "reports.view", "reports.export",
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
        "products.create", "products.view", "products.update", "products.delete",
        "warehouse.manage", "inventory.view", "inventory.adjust", "inventory.transfer",
        "reports.view",
    ],
    "SALES_STAFF": [*_PRODUCT_VIEW_ONLY, "sales.create", "sales.update", "sales.view",
                   "sales.fulfill", "sales.invoice", "sales.payment"],
    "PURCHASE_STAFF": [*_PRODUCT_VIEW_ONLY, "purchases.create", "purchases.update",
                       "purchases.view", "purchases.receive"],
    "ACCOUNTANT": [
        "products.view", "inventory.view",
        "sales.view", "sales.payment", "sales.refund",
        "purchases.view", "purchases.approve", "purchases.return",
        "reports.view", "reports.export", "audit_logs.view",
    ],
    "VIEWER": ["products.view", "inventory.view", "sales.view", "purchases.view",
              "reports.view"],
}
ROLE_NAMES: tuple[str, ...] = tuple(ROLE_PERMISSIONS)

# Old code -> new code, for the Alembic data migration that renames existing
# `permissions.code` rows in place (preserving their id and every
# role_permissions grant pointing at them) rather than deleting and
# re-inserting. See migrations/versions — this dict is the migration's
# source of truth for what changed, kept next to the catalog it describes
# instead of hardcoded a second time inside the migration file.
RENAMED_PERMISSION_CODES: dict[str, str] = {
    "product.create": "products.create",
    "product.read": "products.view",
    "product.update": "products.update",
    "product.delete": "products.delete",
    "inventory.read": "inventory.view",
    "sales.read": "sales.view",
    "purchase.create": "purchases.create",
    "purchase.read": "purchases.view",
    "purchase.update": "purchases.update",
    "purchase.approve": "purchases.approve",
    "purchase.receive": "purchases.receive",
    "purchase.cancel": "purchases.cancel",
    "purchase.return": "purchases.return",
}

# The retired umbrella code, and the granular codes every role that had it
# should be granted instead — see migrations/versions for the data
# migration that applies this to an existing install's role_permissions.
RETIRED_USERS_MANAGE_REPLACEMENT: list[str] = [
    "users.view", "users.create", "users.update", "users.deactivate",
    "users.reset_password", "users.manage_roles",
]
