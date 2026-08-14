"""Purchases page — mirrors sales_page.py; see its docstring."""
from PySide6.QtWidgets import QTableWidget, QVBoxLayout, QWidget

from app.services.billing_service import BillingService
from app.ui.pages.sales_page import _bills_table
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget


class PurchasesPage(QWidget):
    def __init__(self, billing_service: BillingService):
        super().__init__()
        self._billing_service = billing_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Purchases", "Every purchase bill on record."))

        self._async_area = AsyncContentArea(
            load=self._billing_service.list_purchases,
            render=lambda bills: _bills_table(bills),
            is_empty=lambda bills: len(bills) == 0,
            empty_state=EmptyStateWidget(
                "No purchases recorded yet", icon="📥",
                message="Purchases you record will show up here."),
            error_message="Couldn't load purchases.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()
