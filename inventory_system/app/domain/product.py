"""Pure product validation/normalization — Decimal-based, no I/O, no
framework. ProductStatus/ProductType live here (not in app.models or
app.schemas) so both the ORM model and the Pydantic schema share one
source of truth instead of two enums that could drift.
"""
from datetime import date
from decimal import Decimal
from enum import Enum


class ProductStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProductType(str, Enum):
    """Controls which fields are relevant on the Add Product form and, in
    the service layer, whether an opening-stock/warehouse assignment makes
    sense at all: GOODS are physically stocked and inventory-tracked;
    SERVICE and EXPENSE items are not — see
    ProductService.create_product's opening_quantity guard.
    """
    GOODS = "goods"
    SERVICE = "service"
    EXPENSE = "expense"


def normalize_sku(sku: str) -> str:
    """SKUs are stored uppercase-trimmed — mirrors User.email's
    stored-lowercase convention (a CHECK constraint enforces this in the
    database too, so a write that bypasses this function is still caught).
    """
    return sku.strip().upper()


def normalize_barcode(barcode: str | None) -> str | None:
    stripped = barcode.strip() if barcode else ""
    return stripped or None


def normalize_optional_text(value: str | None) -> str | None:
    """Shared blank-to-None normalization for every optional free-text
    product attribute (HSN code, size, color, flavour, DFTQC no., country
    of origin) — one function so "  " and "" are never stored as a
    non-null empty string in any of them.
    """
    stripped = value.strip() if value else ""
    return stripped or None


def validate_product(*, sku: str, name: str, purchase_price: Decimal,
                     selling_price: Decimal, tax_percent: Decimal,
                     minimum_stock_level: Decimal, is_taxable: bool = True,
                     unit_id=None, sub_unit_id=None,
                     sub_unit_conversion_factor: Decimal | None = None,
                     tertiary_unit_id=None,
                     tertiary_unit_conversion_factor: Decimal | None = None,
                     expiry_date: date | None = None) -> list[str]:
    """Returns human-readable error messages; empty list means valid.
    Mirrors the CHECK constraints on the products table so a violation is
    reported clearly by the service layer instead of surfacing as a raw
    IntegrityError from the database.
    """
    errors = []
    if not sku.strip():
        errors.append("SKU is required.")
    if not name.strip():
        errors.append("Name is required.")
    if purchase_price < 0:
        errors.append("Purchase price cannot be negative.")
    if selling_price < 0:
        errors.append("Selling price cannot be negative.")
    if not (Decimal("0") <= tax_percent <= Decimal("100")):
        errors.append("Tax percent must be between 0 and 100.")
    if not is_taxable and tax_percent != Decimal("0"):
        errors.append("A non-taxable product cannot have a tax percent above 0.")
    if minimum_stock_level < 0:
        errors.append("Minimum stock level (re-order point) cannot be negative.")

    # Sub-unit/tertiary-unit conversion: the factor is meaningless without
    # a unit to convert to, and vice versa — both or neither. The factor
    # itself is a real, functional multiplier (base_unit_quantity =
    # sub_unit_quantity * conversion_factor), not a display label, so it
    # must be a positive number.
    if sub_unit_id is not None and sub_unit_conversion_factor is None:
        errors.append("Sub-unit conversion is required when a sub-unit is selected.")
    if sub_unit_id is None and sub_unit_conversion_factor is not None:
        errors.append("Select a sub-unit before entering its conversion factor.")
    if sub_unit_conversion_factor is not None and sub_unit_conversion_factor <= 0:
        errors.append("Sub-unit conversion must be greater than zero.")
    if sub_unit_id is not None and unit_id is not None and sub_unit_id == unit_id:
        errors.append("Sub-unit must be different from the main unit.")

    if tertiary_unit_id is not None and tertiary_unit_conversion_factor is None:
        errors.append("Tertiary unit conversion is required when a tertiary unit is selected.")
    if tertiary_unit_id is None and tertiary_unit_conversion_factor is not None:
        errors.append("Select a tertiary unit before entering its conversion factor.")
    if tertiary_unit_conversion_factor is not None and tertiary_unit_conversion_factor <= 0:
        errors.append("Tertiary unit conversion must be greater than zero.")
    if tertiary_unit_id is not None and unit_id is not None and tertiary_unit_id == unit_id:
        errors.append("Tertiary unit must be different from the main unit.")
    if (tertiary_unit_id is not None and sub_unit_id is not None
            and tertiary_unit_id == sub_unit_id):
        errors.append("Tertiary unit must be different from the sub-unit.")

    if expiry_date is not None and expiry_date < date.today():
        errors.append("Expiry date cannot be in the past.")

    return errors


def validate_opening_stock(*, opening_quantity: Decimal, warehouse_id,
                           product_type: ProductType) -> list[str]:
    """A product must never end up with phantom stock: a positive opening
    quantity with no warehouse to put it in is rejected outright rather
    than silently creating an unassociated Inventory row (or silently
    dropping the quantity) — see ProductService.create_product.
    """
    errors = []
    if opening_quantity < 0:
        errors.append("Opening quantity cannot be negative.")
    if opening_quantity > 0 and warehouse_id is None:
        errors.append("Select a warehouse to receive the opening quantity.")
    if opening_quantity > 0 and product_type != ProductType.GOODS:
        errors.append("Only goods can have an opening quantity — "
                      "services and expenses aren't stocked.")
    return errors
