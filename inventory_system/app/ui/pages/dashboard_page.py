"""Dashboard — the one page with real, live numbers (via DashboardService,
which composes BillingService/StockService/PartyService — themselves
already real against the excel backend). Everything fetched on a
background thread through AsyncContentArea so Argon2/file I/O never
freezes the UI.
"""
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.schemas.dashboard import DashboardSummary
from app.services.dashboard_service import DashboardService
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.stat_card import StatCard
from app.ui.widgets.states import EmptyStateWidget
from app.ui.theme import ACCENT, GREEN, RED


def _money(value) -> str:
    return f"{value:,.2f}"


class DashboardPage(QWidget):
    def __init__(self, dashboard_service: DashboardService):
        super().__init__()
        self._dashboard_service = dashboard_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Dashboard", "An overview of your business right now."))

        self._async_area = AsyncContentArea(
            load=self._dashboard_service.get_summary,
            render=self._render_summary,
            is_empty=lambda summary: (
                summary.total_products_in_stock == 0 and summary.total_parties == 0
                and summary.sales_count == 0 and summary.purchases_count == 0),
            empty_state=EmptyStateWidget(
                "Nothing recorded yet", icon="📊",
                message="Once sales, purchases, or stock exist, they'll summarize here."),
            error_message="Couldn't load the dashboard summary.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    @staticmethod
    def _render_summary(summary: DashboardSummary) -> QWidget:
        container = QWidget()
        container.setContentsMargins(28, 12, 28, 24)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(28, 12, 28, 24)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addWidget(StatCard("Products in Stock", str(summary.total_products_in_stock),
                                 "📦", ACCENT))
        cards.addWidget(StatCard("Parties", str(summary.total_parties), "🤝", ACCENT))
        cards.addWidget(StatCard("Sales", f"{summary.sales_count} bills · "
                                          f"{_money(summary.sales_total)}", "📤", GREEN))
        cards.addWidget(StatCard("Purchases", f"{summary.purchases_count} bills · "
                                              f"{_money(summary.purchases_total)}",
                                 "📥", RED))
        outer.addLayout(cards)
        outer.addStretch()
        return container
