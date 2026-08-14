"""DashboardService tested against fake repositories — no Excel, no database."""
from datetime import date
from decimal import Decimal

from app.schemas.bill import BillCreate, BillLineIn
from app.schemas.party import PartyOut
from app.schemas.stock import StockLevel
from app.services.billing_service import BillingService
from app.services.dashboard_service import DashboardService
from app.services.party_service import PartyService
from app.services.stock_service import StockService
from tests.services.test_billing_service import FakeBillRepo, FakeStockRepo


class FakePartyRepoWithData:
    def __init__(self, parties):
        self._parties = parties

    def upsert_totals(self, pan, name, address, total, is_sale):
        pass

    def list_all(self):
        return self._parties


def _bill(bill_no):
    return BillCreate(date=date(2026, 8, 13), bill_no=bill_no, pan="1", vendor="Acme",
                      address="KTM",
                      lines=[BillLineIn(product="Widget", qty=Decimal("1"),
                                        rate=Decimal("100"))])


def test_get_summary_aggregates_all_three_services():
    sales_repo, purchases_repo = FakeBillRepo(), FakeBillRepo()
    stock_repo = FakeStockRepo()
    stock_repo._levels = {"widget": Decimal("10"), "gadget": Decimal("5")}
    parties = [
        PartyOut(pan="1", name="Acme", address="KTM", total_sales=Decimal("100"),
                total_purchases=Decimal("0"), total_combined=Decimal("100")),
    ]

    billing = BillingService(sales_repo, purchases_repo, stock_repo,
                             FakePartyRepoWithData(parties))
    billing.create_sale(_bill("S-1"))
    billing.create_purchase(_bill("P-1"))
    billing.create_purchase(_bill("P-2"))

    dashboard = DashboardService(billing, StockService(stock_repo),
                                 PartyService(FakePartyRepoWithData(parties)))
    summary = dashboard.get_summary()

    # _bill() lines are qty=1 @ rate=100 with the default 13% VAT -> 113.00/bill
    assert summary.total_products_in_stock == 2
    assert summary.total_parties == 1
    assert summary.sales_count == 1
    assert summary.sales_total == Decimal("113.00")
    assert summary.purchases_count == 2
    assert summary.purchases_total == Decimal("226.00")


def test_get_summary_with_no_data_is_all_zero():
    billing = BillingService(FakeBillRepo(), FakeBillRepo(), FakeStockRepo(),
                             FakePartyRepoWithData([]))
    dashboard = DashboardService(billing, StockService(FakeStockRepo()),
                                 PartyService(FakePartyRepoWithData([])))
    summary = dashboard.get_summary()

    assert summary.total_products_in_stock == 0
    assert summary.sales_count == 0
    assert summary.sales_total == Decimal("0")
