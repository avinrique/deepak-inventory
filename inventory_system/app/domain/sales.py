"""Pure sales validation/enums/status-machine — Decimal-based, no I/O, no
framework. SalesOrderStatus lives here for the same reason as
app.domain.purchasing.PurchaseOrderStatus: the ORM model and the Pydantic
schema share one source of truth instead of two enums that could drift.
"""
import re
from decimal import Decimal
from enum import Enum

from app.domain.pricing import line_subtotal, line_tax, line_total  # noqa: F401 - re-exported

# Mirrors app.domain.user's own _EMAIL_RE/_PHONE_RE rather than importing
# them — each app.domain.* module validates its own fields in isolation
# (no I/O, no cross-module deps), same convention as
# app.domain.purchasing.validate_supplier not reaching into this module
# either. See this module's own docstring.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,32}$")
_CUSTOMER_CODE_RE = re.compile(r"^[A-Z0-9._-]+$")


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
    DIGITAL_WALLET = "DIGITAL_WALLET"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


class InvoicePaymentStatus(str, Enum):
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"


class CustomerType(str, Enum):
    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"


def compute_payment_status(total_amount: Decimal, amount_paid: Decimal) -> InvoicePaymentStatus:
    if amount_paid <= 0:
        return InvoicePaymentStatus.UNPAID
    if amount_paid >= total_amount:
        return InvoicePaymentStatus.PAID
    return InvoicePaymentStatus.PARTIALLY_PAID


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


def normalize_customer_code(code: str) -> str:
    return code.strip().upper()


def validate_customer(*, name: str, customer_code: str | None = None,
                      email: str | None = None, phone: str | None = None,
                      credit_limit: Decimal | None = None,
                      opening_balance: Decimal | None = None) -> list[str]:
    errors = []
    if not name.strip():
        errors.append("Customer name is required.")

    if customer_code is not None:
        normalized_code = normalize_customer_code(customer_code)
        if not normalized_code:
            errors.append("Customer code is required.")
        elif not _CUSTOMER_CODE_RE.match(normalized_code):
            errors.append(
                "Customer code may only contain letters, numbers, '.', '_', and '-'.")

    if email is not None and email.strip() and not _EMAIL_RE.match(email.strip()):
        errors.append("Email address format is invalid.")

    if phone is not None and phone.strip() and not _PHONE_RE.match(phone.strip()):
        errors.append("Phone number format is invalid.")

    if credit_limit is not None and credit_limit < 0:
        errors.append("Credit limit cannot be negative.")

    if opening_balance is not None and opening_balance < 0:
        errors.append("Opening balance cannot be negative.")

    return errors


def validate_sales_order_item(*, quantity_ordered: Decimal, unit_price: Decimal,
                              tax_percent: Decimal,
                              discount_percent: Decimal = Decimal("0"),
                              excise_percent: Decimal = Decimal("0")) -> list[str]:
    errors = []
    if quantity_ordered <= 0:
        errors.append("Quantity must be greater than zero.")
    if unit_price < 0:
        errors.append("Unit price cannot be negative.")
    if not (Decimal("0") <= tax_percent <= Decimal("100")):
        errors.append("Tax percent must be between 0 and 100.")
    if not (Decimal("0") <= discount_percent <= Decimal("100")):
        errors.append("Discount percent must be between 0 and 100.")
    if not (Decimal("0") <= excise_percent <= Decimal("100")):
        errors.append("Excise percent must be between 0 and 100.")
    return errors


# Discount is a sales-specific concept (a supplier price is what it is —
# Purchasing has no discount field), so these live here rather than in the
# shared app.domain.pricing, and are applied *before* tax: tax is charged
# on the discounted price, not the list price, matching standard invoicing
# convention.
def line_discount(quantity: Decimal, unit_price: Decimal, discount_percent: Decimal) -> Decimal:
    return line_subtotal(quantity, unit_price) * discount_percent / Decimal("100")


def line_subtotal_after_discount(quantity: Decimal, unit_price: Decimal,
                                 discount_percent: Decimal) -> Decimal:
    return line_subtotal(quantity, unit_price) - line_discount(quantity, unit_price,
                                                                discount_percent)


def line_tax_after_discount(quantity: Decimal, unit_price: Decimal, discount_percent: Decimal,
                            tax_percent: Decimal) -> Decimal:
    return line_subtotal_after_discount(quantity, unit_price,
                                        discount_percent) * tax_percent / Decimal("100")


# Excise duty — mirrors line_tax_after_discount exactly: applied to the
# same post-discount base as tax, computed independently of tax (neither
# compounds on the other), and simply summed into the line total alongside
# it. See line_total_after_discount.
def line_excise_after_discount(quantity: Decimal, unit_price: Decimal, discount_percent: Decimal,
                               excise_percent: Decimal) -> Decimal:
    return line_subtotal_after_discount(quantity, unit_price,
                                        discount_percent) * excise_percent / Decimal("100")


def line_total_after_discount(quantity: Decimal, unit_price: Decimal, discount_percent: Decimal,
                              tax_percent: Decimal,
                              excise_percent: Decimal = Decimal("0")) -> Decimal:
    after_discount = line_subtotal_after_discount(quantity, unit_price, discount_percent)
    return (after_discount
            + line_tax_after_discount(quantity, unit_price, discount_percent, tax_percent)
            + line_excise_after_discount(quantity, unit_price, discount_percent, excise_percent))


def format_invoice_number(prefix: str, sequence_value: int) -> str:
    """The configurable half of invoice numbering — *what the number looks
    like*. Uniqueness/gaplessness is a different, transactional concern
    handled by the locked per-organization counter in
    app.repositories.sql.sales_repository; this function is pure formatting
    so it's trivially testable on its own.
    """
    return f"{prefix}{sequence_value:06d}"
