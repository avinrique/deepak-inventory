"""Pure billing calculations — Decimal-based, no I/O, no framework.

Mirrors the arithmetic in inventory_app.py's calculate_total()/add_product(),
rewritten on Decimal instead of float so rounding is exact and predictable
for money (float's round() is not: round(2.675, 2) == 2.67 in binary float).
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class BillLine:
    product: str
    qty: Decimal
    rate: Decimal

    @property
    def amount(self) -> Decimal:
        return _money(self.qty * self.rate)


@dataclass(frozen=True)
class BillTotals:
    subtotal: Decimal
    ecs: Decimal
    vat_percent: Decimal
    vat_amount: Decimal
    total: Decimal


def calculate_totals(lines: list[BillLine], ecs: Decimal,
                     vat_percent: Decimal) -> BillTotals:
    subtotal = _money(sum((ln.amount for ln in lines), Decimal("0")))
    vat_amount = _money(subtotal * vat_percent / Decimal("100"))
    total = _money(subtotal + ecs + vat_amount)
    return BillTotals(subtotal, ecs, vat_percent, vat_amount, total)


def validate_line(product: str, qty: Decimal, rate: Decimal) -> str | None:
    """Return an error message, or None if the line is valid.

    Same rules as inventory_app.add_product(): non-blank product, qty > 0,
    rate >= 0. The zero-rate confirm-dialog stays a UI concern, not this
    layer's — this only reports hard errors.
    """
    if not product.strip():
        return "Enter a Product Name."
    if qty <= 0:
        return "Quantity must be greater than 0."
    if rate < 0:
        return "Rate cannot be negative."
    return None


def stock_shortfall(lines: list[BillLine],
                    stock_on_hand: dict[str, Decimal]) -> list[str]:
    """Products in ``lines`` that would go negative given current stock.

    ``stock_on_hand`` is keyed by trimmed-lowercase product name, matching
    storage.update_stock()'s matching rule.
    """
    shortfalls = []
    for ln in lines:
        have = stock_on_hand.get(ln.product.strip().lower(), Decimal("0"))
        if ln.qty > have:
            shortfalls.append(f"{ln.product}: have {have}, selling {ln.qty}")
    return shortfalls
