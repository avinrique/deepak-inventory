from decimal import Decimal

from app.domain.sales import (
    SalesOrderStatus,
    can_transition,
    format_invoice_number,
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
