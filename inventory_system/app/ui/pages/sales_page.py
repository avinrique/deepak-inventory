"""Sales page — real bill list via BillingService.list_sales() (already
fully working against the excel backend). Read-only; creating a sale from
this UI isn't wired up yet (sales.create/cancel/refund permissions exist
in the catalog for when that lands).
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from app.schemas.bill import BillOut
from app.services.billing_service import BillingService
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget


class SalesPage(QWidget):
    def __init__(self, billing_service: BillingService):
        super().__init__()
        self._billing_service = billing_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Sales", "Every sale bill on record."))

        self._async_area = AsyncContentArea(
            load=self._billing_service.list_sales,
            render=lambda bills: _bills_table(bills),
            is_empty=lambda bills: len(bills) == 0,
            empty_state=EmptyStateWidget(
                "No sales recorded yet", icon="📤",
                message="Sales you record will show up here."),
            error_message="Couldn't load sales.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()


def _bills_table(bills: list[BillOut]) -> QTableWidget:
    table = QTableWidget(len(bills), 4)
    table.setHorizontalHeaderLabels(["Date", "Bill No", "Vendor", "Total"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    for row, bill in enumerate(sorted(bills, key=lambda b: b.date, reverse=True)):
        table.setItem(row, 0, QTableWidgetItem(bill.date.strftime("%d/%m/%Y")))
        table.setItem(row, 1, QTableWidgetItem(bill.bill_no or "—"))
        table.setItem(row, 2, QTableWidgetItem(bill.vendor or "—"))
        total_item = QTableWidgetItem(f"{bill.total:,.2f}")
        total_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row, 3, total_item)
    return table
