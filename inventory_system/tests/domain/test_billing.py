from decimal import Decimal

from app.domain.billing import BillLine, calculate_totals, stock_shortfall, validate_line


def test_calculate_totals_basic():
    lines = [BillLine("Widget", Decimal("2"), Decimal("100"))]
    totals = calculate_totals(lines, ecs=Decimal("0"), vat_percent=Decimal("13"))
    assert totals.subtotal == Decimal("200.00")
    assert totals.vat_amount == Decimal("26.00")
    assert totals.total == Decimal("226.00")


def test_calculate_totals_rounds_half_up():
    # 33.335 rounds to 33.34, not float-rounded down to 33.33.
    lines = [BillLine("Widget", Decimal("1"), Decimal("33.335"))]
    totals = calculate_totals(lines, ecs=Decimal("0"), vat_percent=Decimal("13"))
    assert totals.subtotal == Decimal("33.34")


def test_validate_line_rejects_blank_product():
    assert validate_line("", Decimal("1"), Decimal("10")) == "Enter a Product Name."


def test_validate_line_rejects_zero_qty():
    assert validate_line("Widget", Decimal("0"), Decimal("10")) is not None


def test_validate_line_rejects_negative_rate():
    assert validate_line("Widget", Decimal("1"), Decimal("-1")) is not None


def test_validate_line_accepts_valid_line():
    assert validate_line("Widget", Decimal("1"), Decimal("10")) is None


def test_stock_shortfall_flags_oversell():
    lines = [BillLine("Widget", Decimal("5"), Decimal("10"))]
    shortfalls = stock_shortfall(lines, {"widget": Decimal("2")})
    assert len(shortfalls) == 1
    assert "Widget" in shortfalls[0]


def test_stock_shortfall_ignores_sufficient_stock():
    lines = [BillLine("Widget", Decimal("1"), Decimal("10"))]
    assert stock_shortfall(lines, {"widget": Decimal("5")}) == []
