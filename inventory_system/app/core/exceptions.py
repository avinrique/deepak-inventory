"""App-wide exception types, as opposed to bugs — Services raise these for
conditions the UI is expected to catch and show to the user."""
from decimal import Decimal


class AppError(Exception):
    """Base class for expected application-level errors."""


class DuplicateBillError(AppError):
    def __init__(self, bill_no: str):
        self.bill_no = bill_no
        super().__init__(f"Bill No {bill_no!r} already exists")


class InvalidCredentialsError(AppError):
    """Deliberately generic — never reveals whether the email exists, the
    account is deactivated, or the password was wrong."""

    def __init__(self):
        super().__init__("Invalid email or password")


class AccountLockedError(AppError):
    """Raised by AuthService.login instead of InvalidCredentialsError once
    too many failed attempts have been made for an email in a short window
    — see AuthService's module docstring for the lockout policy. Distinct
    from InvalidCredentialsError so the UI can tell someone who already
    knows they've been guessing wrong how long to wait, without that
    message ever appearing for a first wrong guess (which stays a generic
    "invalid credentials", revealing nothing about account existence).
    """
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        minutes = max(1, round(retry_after_seconds / 60))
        super().__init__(
            f"Too many failed login attempts. Try again in {minutes} minute"
            f"{'s' if minutes != 1 else ''}.")


class AmbiguousOrganizationError(AppError):
    """The user belongs to more than one organization and none is marked
    as their default — the caller must specify organization_id."""

    def __init__(self):
        super().__init__(
            "This account belongs to multiple organizations — specify which one to log into")


class ProductValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class DuplicateSkuError(AppError):
    def __init__(self, sku: str):
        self.sku = sku
        super().__init__(f"SKU {sku!r} already exists")


class DuplicateBarcodeError(AppError):
    def __init__(self, barcode: str):
        self.barcode = barcode
        super().__init__(f"Barcode {barcode!r} already exists")


class ProductNotFoundError(AppError):
    def __init__(self, product_id):
        self.product_id = product_id
        super().__init__(f"Product {product_id!r} not found")


class WarehouseNotFoundError(AppError):
    def __init__(self, warehouse_id):
        self.warehouse_id = warehouse_id
        super().__init__(f"Warehouse {warehouse_id!r} not found")


class DuplicateWarehouseCodeError(AppError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Warehouse code {code!r} already exists")


class InventoryValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class InsufficientStockError(AppError):
    """Raised when an operation would take quantity_on_hand (or
    quantity_reserved) below zero and the organization does not allow
    negative stock (Organization.allow_negative_stock).
    """

    def __init__(self, product_id, warehouse_id, available: Decimal, requested: Decimal):
        self.product_id = product_id
        self.warehouse_id = warehouse_id
        self.available = available
        self.requested = requested
        super().__init__(
            f"Insufficient stock for product {product_id!r} at warehouse "
            f"{warehouse_id!r}: available {available}, requested {requested}")


class InvalidTransferError(AppError):
    def __init__(self, message: str):
        super().__init__(message)


class LowStockBlockedError(AppError):
    """Raised when Organization.low_stock_behavior is BLOCK_SALE and a sale
    would take a product's on-hand quantity below its
    Product.minimum_stock_level — distinct from InsufficientStockError:
    there is physically enough stock to fulfill the sale, the organization
    has simply chosen to protect its reorder buffer.
    """

    def __init__(self, product_id, warehouse_id, resulting_quantity: Decimal,
                minimum_stock_level: Decimal):
        self.product_id = product_id
        self.warehouse_id = warehouse_id
        self.resulting_quantity = resulting_quantity
        self.minimum_stock_level = minimum_stock_level
        super().__init__(
            f"Fulfilling this would leave product {product_id!r} at warehouse "
            f"{warehouse_id!r} at {resulting_quantity}, below its minimum stock level of "
            f"{minimum_stock_level}.")


class SupplierNotFoundError(AppError):
    def __init__(self, supplier_id):
        self.supplier_id = supplier_id
        super().__init__(f"Supplier {supplier_id!r} not found")


class PurchaseOrderNotFoundError(AppError):
    def __init__(self, purchase_order_id):
        self.purchase_order_id = purchase_order_id
        super().__init__(f"Purchase order {purchase_order_id!r} not found")


class PurchaseOrderItemNotFoundError(AppError):
    def __init__(self, purchase_order_item_id):
        self.purchase_order_item_id = purchase_order_item_id
        super().__init__(f"Purchase order item {purchase_order_item_id!r} not found")


class PurchaseOrderValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class InvalidPurchaseOrderTransitionError(AppError):
    def __init__(self, current, target):
        self.current = current
        self.target = target
        super().__init__(f"Cannot move purchase order from {current!r} to {target!r}")


class CustomerNotFoundError(AppError):
    def __init__(self, customer_id):
        self.customer_id = customer_id
        super().__init__(f"Customer {customer_id!r} not found")


class SalesOrderNotFoundError(AppError):
    def __init__(self, sales_order_id):
        self.sales_order_id = sales_order_id
        super().__init__(f"Sales order {sales_order_id!r} not found")


class SalesOrderItemNotFoundError(AppError):
    def __init__(self, sales_order_item_id):
        self.sales_order_item_id = sales_order_item_id
        super().__init__(f"Sales order item {sales_order_item_id!r} not found")


class SalesOrderValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class InvalidSalesOrderTransitionError(AppError):
    def __init__(self, current, target):
        self.current = current
        self.target = target
        super().__init__(f"Cannot move sales order from {current!r} to {target!r}")


class InvoiceNotFoundError(AppError):
    def __init__(self, invoice_id):
        self.invoice_id = invoice_id
        super().__init__(f"Invoice {invoice_id!r} not found")


class DuplicateInvoiceError(AppError):
    def __init__(self, sales_order_id):
        self.sales_order_id = sales_order_id
        super().__init__(f"Sales order {sales_order_id!r} already has an invoice")


class OrganizationNotFoundError(AppError):
    def __init__(self, organization_id):
        self.organization_id = organization_id
        super().__init__(f"Organization {organization_id!r} not found")


class OrganizationValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class MembershipNotFoundError(AppError):
    def __init__(self, user_id, organization_id):
        self.user_id = user_id
        self.organization_id = organization_id
        super().__init__(f"User {user_id!r} has no membership in organization "
                         f"{organization_id!r}")


class BackupNotFoundError(AppError):
    def __init__(self, backup_id):
        self.backup_id = backup_id
        super().__init__(f"Backup {backup_id!r} not found")


class BackupFailedError(AppError):
    """error_message is already sanitized by app.backup.postgres_backup —
    safe to display to the user as-is."""
    def __init__(self, error_message: str | None):
        self.error_message = error_message
        super().__init__(error_message or "Backup failed.")


class BackupNotVerifiedError(AppError):
    def __init__(self, backup_id):
        self.backup_id = backup_id
        super().__init__(f"Backup {backup_id!r} has not passed verification — refusing to "
                         "restore from it. Re-run verification or choose another backup.")


class BackupRestoreFailedError(AppError):
    """error_message is already sanitized by app.backup.postgres_backup —
    safe to display to the user as-is."""
    def __init__(self, error_message: str | None):
        self.error_message = error_message
        super().__init__(error_message or "Restore failed.")


class PasswordPolicyViolationError(AppError):
    """Raised by UserService.create_user/AuthService.change_password when a
    user-supplied password doesn't meet the organization's configured
    password policy (see app.domain.security_policy)."""
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class UserNotFoundError(AppError):
    def __init__(self, user_id):
        self.user_id = user_id
        super().__init__(f"User {user_id!r} not found")


class DuplicateEmailError(AppError):
    def __init__(self, email: str):
        self.email = email
        super().__init__(f"Email {email!r} already exists")


class DuplicateUsernameError(AppError):
    def __init__(self, username: str):
        self.username = username
        super().__init__(f"Username {username!r} already exists")


class OwnerProtectedError(AppError):
    """Raised when an operation would deactivate or demote an
    organization's Owner — see app.security.permissions's module docstring:
    "an Owner can't be removed/demoted, there's exactly one per
    organization" is an invariant enforced here, not just documented.
    """
    def __init__(self, user_id):
        self.user_id = user_id
        super().__init__(f"User {user_id!r} is this organization's Owner and cannot be "
                         "deactivated or demoted")


class UserValidationError(AppError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class RoleNotFoundError(AppError):
    def __init__(self, role_id):
        self.role_id = role_id
        super().__init__(f"Role {role_id!r} not found")
