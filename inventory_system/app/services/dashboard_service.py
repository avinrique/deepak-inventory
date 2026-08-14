"""Application service aggregating stats for the Dashboard page — one
Service call from the UI (not three), composing BillingService/
StockService/PartyService rather than the page composing them itself.
"""
from decimal import Decimal

from app.schemas.dashboard import DashboardSummary
from app.services.billing_service import BillingService
from app.services.party_service import PartyService
from app.services.stock_service import StockService


class DashboardService:
    def __init__(self, billing: BillingService, stock: StockService, parties: PartyService):
        self._billing = billing
        self._stock = stock
        self._parties = parties

    def get_summary(self) -> DashboardSummary:
        stock_levels = self._stock.list_stock()
        party_list = self._parties.search()
        sales = self._billing.list_sales()
        purchases = self._billing.list_purchases()
        return DashboardSummary(
            total_products_in_stock=len(stock_levels),
            total_parties=len(party_list),
            sales_count=len(sales),
            sales_total=sum((b.total for b in sales), Decimal("0")),
            purchases_count=len(purchases),
            purchases_total=sum((b.total for b in purchases), Decimal("0")),
        )
