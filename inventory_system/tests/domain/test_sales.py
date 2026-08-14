from decimal import Decimal

from app.domain.sales import (
    SalesOrderStatus,
    can_transition,
    format_invoice_number,
    line_discount,
    line_subtotal_after_discount,
    line_tax_after_discount,
    line_total_after_discount,
    validate_customer,
    validate_sales_order_item,
)


def test_draft_can_move_to_confirmed_or_cancelled():
    assert can_transition(SalesOrderStatus.DRAFT, SalesOrderStatus.CONFIRMED)
    assert can_transition(SalesOrderStatus.DRAFT, SalesOrderStatus.CANCELLED)
    assert not can_transition(SalesOrderStatus.DRAFT, SalesOrderStatus.FULFILLED)
    assert not can_transition(SalesOrderStatus.DRAFT, SalesOrderStatus.COMPLETED)


def test_confirmed_can_move_to_fulfilled_or_cancelled():
    assert can_transition(SalesOrderStatus.CONFIRMED, SalesOrderStatus.FULFILLED)
    assert can_transition(SalesOrderStatus.CONFIRMED, SalesOrderStatus.CANCELLED)
    assert not can_transition(SalesOrderStatus.CONFIRMED, SalesOrderStatus.DRAFT)
    assert not can_transition(SalesOrderStatus.CONFIRMED, SalesOrderStatus.COMPLETED)


def test_fulfilled_can_only_reach_completed_not_cancelled():
    assert can_transition(SalesOrderStatus.FULFILLED, SalesOrderStatus.COMPLETED)
    assert not can_transition(SalesOrderStatus.FULFILLED, SalesOrderStatus.CANCELLED)
    assert not can_transition(SalesOrderStatus.FULFILLED, SalesOrderStatus.CONFIRMED)


def test_completed_and_cancelled_are_terminal():
    for status in SalesOrderStatus:
        assert not can_transition(SalesOrderStatus.COMPLETED, status)
        assert not can_transition(SalesOrderStatus.CANCELLED, status)


def test_validate_customer_requires_name():
    assert validate_customer(name="") != []
    assert validate_customer(name="Acme") == []


def test_validate_sales_order_item_rejects_non_positive_quantity():
    errors = validate_sales_order_item(quantity_ordered=Decimal("0"),
                                       unit_price=Decimal("10"), tax_percent=Decimal("0"))
    assert errors != []


def test_validate_sales_order_item_rejects_negative_price():
    errors = validate_sales_order_item(quantity_ordered=Decimal("5"),
                                       unit_price=Decimal("-1"), tax_percent=Decimal("0"))
    assert errors != []


def test_validate_sales_order_item_rejects_out_of_range_tax():
    errors = validate_sales_order_item(quantity_ordered=Decimal("5"),
                                       unit_price=Decimal("10"), tax_percent=Decimal("150"))
    assert errors != []


def test_validate_sales_order_item_accepts_valid_input():
    assert validate_sales_order_item(quantity_ordered=Decimal("5"), unit_price=Decimal("10"),
                                     tax_percent=Decimal("13")) == []


def test_format_invoice_number_zero_pads_and_prefixes():
    assert format_invoice_number("INV-", 1) == "INV-000001"
    assert format_invoice_number("INV-", 42) == "INV-000042"
    assert format_invoice_number("ACME/", 123456) == "ACME/123456"


def test_validate_sales_order_item_rejects_out_of_range_discount():
    errors = validate_sales_order_item(quantity_ordered=Decimal("5"), unit_price=Decimal("10"),
                                       tax_percent=Decimal("0"),
                                       discount_percent=Decimal("101"))
    assert errors != []


def test_validate_sales_order_item_accepts_valid_discount():
    assert validate_sales_order_item(quantity_ordered=Decimal("5"), unit_price=Decimal("10"),
                                     tax_percent=Decimal("13"),
                                     discount_percent=Decimal("10")) == []


def test_validate_sales_order_item_defaults_discount_to_zero():
    # No discount_percent passed at all — should not error, matching the
    # domain function's default of Decimal("0").
    assert validate_sales_order_item(quantity_ordered=Decimal("5"), unit_price=Decimal("10"),
                                     tax_percent=Decimal("13")) == []


def test_line_discount_is_a_percentage_of_the_list_subtotal():
    # 10 units x 100 = 1000 list price; 10% discount = 100 off.
    assert line_discount(Decimal("10"), Decimal("100"), Decimal("10")) == Decimal("100")


def test_line_discount_zero_percent_is_zero():
    assert line_discount(Decimal("10"), Decimal("100"), Decimal("0")) == Decimal("0")


def test_line_subtotal_after_discount():
    assert line_subtotal_after_discount(Decimal("10"), Decimal("100"),
                                        Decimal("10")) == Decimal("900")


def test_tax_is_computed_on_the_discounted_price_not_the_list_price():
    # 1000 list, 10% discount -> 900 taxable base, 13% tax -> 117, not 130.
    tax = line_tax_after_discount(Decimal("10"), Decimal("100"), Decimal("10"), Decimal("13"))
    assert tax == Decimal("117")


def test_line_total_after_discount_combines_discount_and_tax():
    total = line_total_after_discount(Decimal("10"), Decimal("100"), Decimal("10"),
                                      Decimal("13"))
    assert total == Decimal("1017")   # 900 (after discount) + 117 (tax on 900)


def test_zero_discount_matches_undiscounted_totals():
    # With discount_percent=0, the discount-aware helpers must reduce to
    # exactly what an un-discounted line would have been — no silent
    # behavior change for the many existing orders that never use discount.
    from app.domain.pricing import line_tax, line_total
    qty, price, tax_pct = Decimal("7"), Decimal("42.50"), Decimal("13")
    assert line_subtotal_after_discount(qty, price, Decimal("0")) == qty * price
    assert line_tax_after_discount(qty, price, Decimal("0"), tax_pct) == line_tax(qty, price,
                                                                                  tax_pct)
    assert line_total_after_discount(qty, price, Decimal("0"), tax_pct) == line_total(
        qty, price, tax_pct)
