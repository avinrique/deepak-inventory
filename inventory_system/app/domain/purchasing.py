"""Pure purchasing validation/enums/status-machine — Decimal-based, no I/O,
no framework. PurchaseOrderStatus lives here (not app.models or
app.schemas) for the same reason as app.domain.product.ProductStatus and
app.domain.inventory.InventoryTransactionType: the ORM model and the
Pydantic schema share one source of truth instead of two enums that could
drift.
"""
import re
from decimal import Decimal
from enum import Enum

from app.domain.pricing import line_subtotal, line_tax, line_total  # noqa: F401 - re-exported

# Mirrors app.domain.sales/app.domain.user's own _EMAIL_RE/_PHONE_RE rather
# than importing them — this module stays dependency-free, same convention
# app.domain.sales's own copy documents.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,32}$")


class PurchaseOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


# Allowed forward transitions for the *explicit* actions (submit / approve /
# cancel). PARTIALLY_RECEIVED and RECEIVED are deliberately not reachable
# through a generic "transition to X" call — receive_goods() computes
# which of the two applies from the resulting quantities rather than the
# caller picking a target, so those two only appear here as the values
# APPROVED/PARTIALLY_RECEIVED are allowed to reach, not as transitions
# PurchaseService exposes directly. See PurchaseService.receive_goods.
ALLOWED_TRANSITIONS: dict[PurchaseOrderStatus, frozenset[PurchaseOrderStatus]] = {
    PurchaseOrderStatus.DRAFT: frozenset({
        PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.CANCELLED}),
    PurchaseOrderStatus.SUBMITTED: frozenset({
        PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CANCELLED}),
    PurchaseOrderStatus.APPROVED: frozenset({
        PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.CANCELLED}),
    PurchaseOrderStatus.PARTIALLY_RECEIVED: frozenset({
        PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED}),
    PurchaseOrderStatus.RECEIVED: frozenset(),
    PurchaseOrderStatus.CANCELLED: frozenset(),
}


def can_transition(current: PurchaseOrderStatus, target: PurchaseOrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_supplier(*, name: str, email: str | None = None,
                      phone: str | None = None) -> list[str]:
    errors = []
    if not name.strip():
        errors.append("Supplier name is required.")
    if email is not None and email.strip() and not _EMAIL_RE.match(email.strip()):
        errors.append("Email address format is invalid.")
    if phone is not None and phone.strip() and not _PHONE_RE.match(phone.strip()):
        errors.append("Phone number format is invalid.")
    return errors


def validate_purchase_order_item(*, quantity_ordered: Decimal, unit_price: Decimal,
                                 tax_percent: Decimal) -> list[str]:
    errors = []
    if quantity_ordered <= 0:
        errors.append("Quantity ordered must be greater than zero.")
    if unit_price < 0:
        errors.append("Unit price cannot be negative.")
    if not (Decimal("0") <= tax_percent <= Decimal("100")):
        errors.append("Tax percent must be between 0 and 100.")
    return errors


def format_purchase_order_number(prefix: str, sequence_value: int) -> str:
    """The configurable half of purchase order numbering — mirrors
    app.domain.sales.format_invoice_number exactly. Uniqueness/
    gaplessness is a transactional concern handled by the locked
    per-organization counter in app.repositories.sql.purchase_repository
    (see PurchaseOrderSequence); this function is pure formatting.
    """
    return f"{prefix}{sequence_value:06d}"


