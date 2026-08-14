from decimal import Decimal

import pytest

from app.domain.inventory import (
    InventoryTransactionType,
    normalize_warehouse_code,
    signed_quantity_change,
    validate_quantity,
    validate_warehouse,
)


def test_signed_quantity_change_positive_for_in_types():
    assert signed_quantity_change(InventoryTransactionType.STOCK_IN, Decimal("5")) == Decimal("5")
    assert signed_quantity_change(InventoryTransactionType.RETURN_IN, Decimal("5")) == Decimal("5")
    assert signed_quantity_change(InventoryTransactionType.TRANSFER_IN, Decimal("5")) == Decimal("5")
    assert signed_quantity_change(InventoryTransactionType.RESERVE, Decimal("5")) == Decimal("5")


def test_signed_quantity_change_negative_for_out_types():
    assert signed_quantity_change(InventoryTransactionType.STOCK_OUT, Decimal("5")) == Decimal("-5")
    assert signed_quantity_change(InventoryTransactionType.SALE, Decimal("5")) == Decimal("-5")
    assert signed_quantity_change(InventoryTransactionType.DAMAGE, Decimal("5")) == Decimal("-5")
    assert signed_quantity_change(InventoryTransactionType.RETURN_OUT, Decimal("5")) == Decimal("-5")
    assert signed_quantity_change(InventoryTransactionType.TRANSFER_OUT, Decimal("5")) == Decimal("-5")
    assert signed_quantity_change(InventoryTransactionType.RELEASE_RESERVE, Decimal("5")) == Decimal("-5")


def test_signed_quantity_change_adjustment_passes_through_unchanged():
    assert signed_quantity_change(InventoryTransactionType.ADJUSTMENT, Decimal("-3")) == Decimal("-3")
    assert signed_quantity_change(InventoryTransactionType.ADJUSTMENT, Decimal("3")) == Decimal("3")


def test_validate_quantity_rejects_zero():
    assert validate_quantity(Decimal("0")) != []


def test_validate_quantity_rejects_negative_by_default():
    errors = validate_quantity(Decimal("-1"))
    assert errors != []


def test_validate_quantity_allows_negative_when_flagged():
    assert validate_quantity(Decimal("-1"), allow_negative=True) == []


def test_validate_quantity_accepts_positive():
    assert validate_quantity(Decimal("5")) == []


def test_validate_warehouse_requires_code_and_name():
    errors = validate_warehouse(code="", name="")
    assert len(errors) == 2


def test_validate_warehouse_accepts_valid_input():
    assert validate_warehouse(code="MAIN", name="Main Warehouse") == []


def test_normalize_warehouse_code_uppercases_and_trims():
    assert normalize_warehouse_code("  main-1  ") == "MAIN-1"
