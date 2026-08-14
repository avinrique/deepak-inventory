"""BillingService tested against hand-written fake repositories implementing
the repositories/interfaces.py Protocols — no Excel, no database.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.schemas.bill import BillCreate, BillLineIn, BillOut
from app.schemas.stock import StockLevel
from app.services.billing_service import BillingService, DuplicateBillError


class FakeBillRepo:
    def __init__(self):
        self.appended = []
        self._bill_nos = set()

    def append(self, bill, subtotal, vat_amount, total):
        self.appended.append((bill, subtotal, vat_amount, total))
        self._bill_nos.add(bill.bill_no)

    def exists(self, bill_no):
        return bill_no in self._bill_nos

    def list_all(self):
        return [
            BillOut(date=bill.date, bill_no=bill.bill_no, pan=bill.pan, vendor=bill.vendor,
                   address=bill.address, lines=bill.lines, subtotal=subtotal, ecs=bill.ecs,
                   vat_percent=bill.vat_percent, vat_amount=vat_amount, total=total)
            for bill, subtotal, vat_amount, total in self.appended
        ]


class FakeStockRepo:
    def __init__(self):
        self.adjustments = []
        self._levels = {}

    def on_hand(self, product):
        return self._levels.get(product.strip().lower(), Decimal("0"))

    def adjust(self, product, qty, add):
        key = product.strip().lower()
        current = self._levels.get(key, Decimal("0"))
        self._levels[key] = current + qty if add else current - qty
        self.adjustments.append((product, qty, add))
        return self._levels[key]

    def product_names(self):
        return list(self._levels)

    def list_all(self):
        return [StockLevel(product=name, quantity=qty) for name, qty in self._levels.items()]


class FakePartyRepo:
    def __init__(self):
        self.upserts = []

    def upsert_totals(self, pan, name, address, total, is_sale):
        self.upserts.append((pan, name, address, total, is_sale))

    def list_all(self):
        return []


def _bill(**overrides):
    defaults = dict(date=date(2026, 8, 13), bill_no="B-1", pan="123", vendor="Acme",
                    address="KTM",
                    lines=[BillLineIn(product="Widget", qty=Decimal("2"),
                                      rate=Decimal("50"))])
    defaults.update(overrides)
    return BillCreate(**defaults)


def _service(sales=None, purchases=None, stock=None, parties=None):
    return BillingService(sales or FakeBillRepo(), purchases or FakeBillRepo(),
                          stock or FakeStockRepo(), parties or FakePartyRepo())


def test_create_sale_computes_totals_and_updates_stock_and_party():
    stock, parties = FakeStockRepo(), FakePartyRepo()
    service = _service(stock=stock, parties=parties)
    out = service.create_sale(_bill())

    assert out.total == Decimal("113.00")
    assert stock.adjustments == [("Widget", Decimal("2"), False)]
    assert parties.upserts[0][3] == Decimal("113.00")
    assert parties.upserts[0][4] is True


def test_create_purchase_adds_to_stock():
    stock = FakeStockRepo()
    service = _service(stock=stock)
    service.create_purchase(_bill(bill_no="B-2"))
    assert stock.adjustments == [("Widget", Decimal("2"), True)]


def test_sale_and_purchase_write_to_separate_repositories():
    sales, purchases = FakeBillRepo(), FakeBillRepo()
    service = _service(sales=sales, purchases=purchases)
    service.create_sale(_bill(bill_no="S-1"))
    service.create_purchase(_bill(bill_no="P-1"))

    assert sales.exists("S-1") and not sales.exists("P-1")
    assert purchases.exists("P-1") and not purchases.exists("S-1")


def test_duplicate_bill_no_rejected_by_default():
    sales = FakeBillRepo()
    service = _service(sales=sales)
    service.create_sale(_bill(bill_no="B-1"))
    with pytest.raises(DuplicateBillError):
        service.create_sale(_bill(bill_no="B-1"))


def test_duplicate_bill_no_allowed_when_flagged():
    service = _service()
    service.create_sale(_bill(bill_no="B-1"))
    service.create_sale(_bill(bill_no="B-1"), allow_duplicate=True)  # should not raise


def test_duplicate_check_is_per_ledger_not_global():
    # Same Bill No as a sale and as a purchase is legitimate — they're
    # different ledgers, so this must not raise DuplicateBillError.
    service = _service()
    service.create_sale(_bill(bill_no="B-1"))
    service.create_purchase(_bill(bill_no="B-1"))


def test_check_shortfall_flags_oversell():
    service = _service()
    shortfalls = service.check_shortfall(_bill())
    assert len(shortfalls) == 1


def test_list_sales_returns_only_sales():
    sales, purchases = FakeBillRepo(), FakeBillRepo()
    service = _service(sales=sales, purchases=purchases)
    service.create_sale(_bill(bill_no="S-1"))
    service.create_purchase(_bill(bill_no="P-1"))

    result = service.list_sales()

    assert len(result) == 1
    assert result[0].bill_no == "S-1"


def test_list_purchases_returns_only_purchases():
    sales, purchases = FakeBillRepo(), FakeBillRepo()
    service = _service(sales=sales, purchases=purchases)
    service.create_sale(_bill(bill_no="S-1"))
    service.create_purchase(_bill(bill_no="P-1"))

    result = service.list_purchases()

    assert len(result) == 1
    assert result[0].bill_no == "P-1"
