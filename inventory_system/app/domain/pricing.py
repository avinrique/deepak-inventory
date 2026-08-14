"""Line-item money math shared by Purchasing and Sales — quantity * price
(+ tax), nothing purchase- or sales-specific about the arithmetic itself.
Decimal in, Decimal out; never float (see the Decimal precedent set
throughout app.domain / app.models).
"""
from decimal import Decimal


def line_subtotal(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return quantity * unit_price


def line_tax(quantity: Decimal, unit_price: Decimal, tax_percent: Decimal) -> Decimal:
    return quantity * unit_price * tax_percent / Decimal("100")


def line_total(quantity: Decimal, unit_price: Decimal, tax_percent: Decimal) -> Decimal:
    return line_subtotal(quantity, unit_price) + line_tax(quantity, unit_price, tax_percent)
