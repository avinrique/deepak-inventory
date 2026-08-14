"""Pure sales validation/enums/status-machine — Decimal-based, no I/O, no
framework. SalesOrderStatus lives here for the same reason as
app.domain.purchasing.PurchaseOrderStatus: the ORM model and the Pydantic
schema share one source of truth instead of two enums that could drift.
"""
from decimal import Decimal
from enum import Enum

from app.domain.pricing import line_subtotal, line_tax, line_total  # noqa: F401 - re-exported


class SalesOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    FULFILLED = "FULFILLED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PaymentMethod(str, Enum):
    CASH = "CASH"
    CARD = "CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


# Allowed forward transitions for the explicit actions (confirm / fulfill /
# cancel). FULFILLED -> COMPLETED is deliberately not reachable through a
# generic "transition to X" call — it only happens as a side effect of
# record_payment once an invoice is fully paid, so there is no
# complete_sale() action exposed at all; see SalesService.record_payment
# and SalesOrderRepository.record_payment.
ALLOWED_TRANSITIONS: dict[SalesOrderStatus, frozenset[SalesOrderStatus]] = {
    SalesOrderStatus.DRAFT: frozenset({SalesOrderStatus.CONFIRMED, SalesOrderStatus.CANCELLED}),
    SalesOrderStatus.CONFIRMED: frozenset({
        SalesOrderStatus.FULFILLED, SalesOrderStatus.CANCELLED}),
    SalesOrderStatus.FULFILLED: frozenset({SalesOrderStatus.COMPLETED}),
    SalesOrderStatus.COMPLETED: frozenset(),
    SalesOrderStatus.CANCELLED: frozenset(),
}


def can_transition(current: SalesOrderStatus, target: SalesOrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_customer(*, name: str) -> list[str]:
    errors = []
    if not name.strip():
        errors.append("Customer name is required.")
    return errors


def validate_sales_order_item(*, quantity_ordered: Decimal, unit_price: Decimal,
                              tax_percent: Decimal) -> list[str]:
    errors = []
    if quantity_ordered <= 0:
        errors.append("Quantity must be greater than zero.")
    if unit_price < 0:
        errors.append("Unit price cannot be negative.")
    if not (Decimal("0") <= tax_percent <= Decimal("100")):
        errors.append("Tax percent must be between 0 and 100.")
    return errors


def format_invoice_number(prefix: str, sequence_value: int) -> str:
    """The configurable half of invoice numbering — *what the number looks
    like*. Uniqueness/gaplessness is a different, transactional concern
    handled by the locked per-organization counter in
    app.repositories.sql.sales_repository; this function is pure formatting
    so it's trivially testable on its own.
    """
    return f"{prefix}{sequence_value:06d}"
