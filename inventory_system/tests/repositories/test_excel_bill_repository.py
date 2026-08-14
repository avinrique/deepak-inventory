"""Exercises ExcelBillRepository against the real, untouched storage.py in a
temp directory. Once app/repositories/sql/bill_repository.py is implemented
(Phase 2), the same test bodies should run again against it as a contract
test proving parity — see docs/architecture.md.
"""
import importlib
from datetime import date
from decimal import Decimal

import pytest


@pytest.fixture()
def excel_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_DATA_DIR", str(tmp_path))

    # Importing the excel package first runs its sys.path shim (see
    # app/repositories/excel/__init__.py), which is what makes the legacy
    # app's storage.py importable at all from inside inventory_system/.
    from app.repositories.excel.bill_repository import ExcelBillRepository
    import storage
    importlib.reload(storage)  # re-read INVENTORY_DATA_DIR into module constants

    return ExcelBillRepository(storage.SALES_FILE)


def test_append_then_exists(excel_repo):
    from app.schemas.bill import BillCreate, BillLineIn

    bill = BillCreate(date=date(2026, 8, 13), bill_no="B-1", pan="123", vendor="Acme",
                      address="Kathmandu",
                      lines=[BillLineIn(product="Widget", qty=Decimal("2"),
                                        rate=Decimal("50"))])
    excel_repo.append(bill, subtotal=Decimal("100"), vat_amount=Decimal("13"),
                      total=Decimal("113"))

    assert excel_repo.exists("B-1") is True
    assert excel_repo.exists("B-999") is False


def test_list_all_returns_empty_when_no_bills(excel_repo):
    assert excel_repo.list_all() == []


def test_list_all_groups_rows_back_into_bills(excel_repo):
    from app.schemas.bill import BillCreate, BillLineIn

    two_line_bill = BillCreate(
        date=date(2026, 8, 13), bill_no="B-1", pan="123", vendor="Acme",
        address="Kathmandu",
        lines=[BillLineIn(product="Widget", qty=Decimal("2"), rate=Decimal("50")),
              BillLineIn(product="Gadget", qty=Decimal("1"), rate=Decimal("30"))])
    excel_repo.append(two_line_bill, subtotal=Decimal("130"), vat_amount=Decimal("16.9"),
                      total=Decimal("146.9"))

    other_bill = BillCreate(
        date=date(2026, 8, 14), bill_no="B-2", pan="456", vendor="Beta",
        address="Pokhara",
        lines=[BillLineIn(product="Widget", qty=Decimal("1"), rate=Decimal("50"))])
    excel_repo.append(other_bill, subtotal=Decimal("50"), vat_amount=Decimal("6.5"),
                      total=Decimal("56.5"))

    bills = excel_repo.list_all()

    assert len(bills) == 2
    first = next(b for b in bills if b.bill_no == "B-1")
    assert first.date == date(2026, 8, 13)
    assert first.vendor == "Acme"
    assert len(first.lines) == 2
    assert first.total == Decimal("146.9")
    second = next(b for b in bills if b.bill_no == "B-2")
    assert second.total == Decimal("56.5")
    assert len(second.lines) == 1
