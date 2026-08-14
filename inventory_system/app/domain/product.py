"""Pure product validation/normalization — Decimal-based, no I/O, no
framework. ProductStatus lives here (not in app.models or app.schemas) so
both the ORM model and the Pydantic schema share one source of truth
instead of two enums that could drift.
"""
from decimal import Decimal
from enum import Enum


class ProductStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


def normalize_sku(sku: str) -> str:
    """SKUs are stored uppercase-trimmed — mirrors User.email's
    stored-lowercase convention (a CHECK constraint enforces this in the
    database too, so a write that bypasses this function is still caught).
    """
    return sku.strip().upper()


def normalize_barcode(barcode: str | None) -> str | None:
    stripped = barcode.strip() if barcode else ""
    return stripped or None


def validate_product(*, sku: str, name: str, purchase_price: Decimal,
                     selling_price: Decimal, tax_percent: Decimal,
                     minimum_stock_level: Decimal) -> list[str]:
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
    if minimum_stock_level < 0:
        errors.append("Minimum stock level cannot be negative.")
    return errors
