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
