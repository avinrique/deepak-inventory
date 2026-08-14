"""App-wide exception types, as opposed to bugs — Services raise these for
conditions the UI is expected to catch and show to the user."""


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
