from decimal import Decimal

from app.domain.purchasing import (
    PurchaseOrderStatus,
    can_transition,
    line_subtotal,
    line_tax,
    line_total,
    validate_purchase_order_item,
    validate_supplier,
)


def test_draft_can_move_to_submitted_or_cancelled():
    assert can_transition(PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SUBMITTED)
    assert can_transition(PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.CANCELLED)
    assert not can_transition(PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.APPROVED)
    assert not can_transition(PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.RECEIVED)


def test_submitted_can_move_to_approved_or_cancelled():
    assert can_transition(PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.APPROVED)
    assert can_transition(PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.CANCELLED)
    assert not can_transition(PurchaseOrderStatus.SUBMITTED, PurchaseOrderStatus.DRAFT)


def test_approved_can_move_to_received_states_or_cancelled():
    assert can_transition(PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.PARTIALLY_RECEIVED)
    assert can_transition(PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.RECEIVED)
    assert can_transition(PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CANCELLED)


def test_partially_received_can_only_progress_to_received_or_stay():
    assert can_transition(PurchaseOrderStatus.PARTIALLY_RECEIVED,
                          PurchaseOrderStatus.PARTIALLY_RECEIVED)
    assert can_transition(PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED)
    assert not can_transition(PurchaseOrderStatus.PARTIALLY_RECEIVED,
                              PurchaseOrderStatus.CANCELLED)
    assert not can_transition(PurchaseOrderStatus.PARTIALLY_RECEIVED,
                              PurchaseOrderStatus.APPROVED)


def test_received_and_cancelled_are_terminal():
    for status in PurchaseOrderStatus:
        assert not can_transition(PurchaseOrderStatus.RECEIVED, status)
        assert not can_transition(PurchaseOrderStatus.CANCELLED, status)


def test_validate_supplier_requires_name():
    assert validate_supplier(name="") != []
    assert validate_supplier(name="Acme") == []


def test_validate_purchase_order_item_rejects_non_positive_quantity():
    errors = validate_purchase_order_item(quantity_ordered=Decimal("0"),
                                          unit_price=Decimal("10"), tax_percent=Decimal("0"))
    assert errors != []


def test_validate_purchase_order_item_rejects_negative_price():
    errors = validate_purchase_order_item(quantity_ordered=Decimal("5"),
                                          unit_price=Decimal("-1"), tax_percent=Decimal("0"))
    assert errors != []


def test_validate_purchase_order_item_rejects_out_of_range_tax():
    errors = validate_purchase_order_item(quantity_ordered=Decimal("5"),
                                          unit_price=Decimal("10"), tax_percent=Decimal("150"))
    assert errors != []


def test_validate_purchase_order_item_accepts_valid_input():
    assert validate_purchase_order_item(quantity_ordered=Decimal("5"),
                                        unit_price=Decimal("10"),
                                        tax_percent=Decimal("13")) == []


def test_line_totals():
    qty, price, tax = Decimal("10"), Decimal("100"), Decimal("13")
    assert line_subtotal(qty, price) == Decimal("1000")
    assert line_tax(qty, price, tax) == Decimal("130.00")
    assert line_total(qty, price, tax) == Decimal("1130.00")
