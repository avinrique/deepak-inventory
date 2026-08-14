"""Inventory page — real stock levels via StockService (already fully
working against the excel backend). Read-only list; adjustments/transfers
aren't wired up yet (inventory.adjust/inventory.transfer permissions exist
in the catalog for when that lands).
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from app.schemas.stock import StockLevel
from app.services.stock_service import StockService
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget


class InventoryPage(QWidget):
    def __init__(self, stock_service: StockService):
        super().__init__()
        self._stock_service = stock_service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Inventory", "Current quantity on hand, per product."))

        self._async_area = AsyncContentArea(
            load=self._stock_service.list_stock,
            render=self._render_table,
            is_empty=lambda levels: len(levels) == 0,
            empty_state=EmptyStateWidget(
                "No stock recorded yet", icon="📊",
                message="Products appear here automatically once a sale or "
                        "purchase is recorded."),
            error_message="Couldn't load stock levels.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    @staticmethod
    def _render_table(levels: list[StockLevel]) -> QTableWidget:
        table = QTableWidget(len(levels), 2)
        table.setHorizontalHeaderLabels(["Product", "Quantity"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setContentsMargins(28, 12, 28, 24)
        for row, level in enumerate(sorted(levels, key=lambda l: l.product.lower())):
            table.setItem(row, 0, QTableWidgetItem(level.product))
            qty_item = QTableWidgetItem(f"{level.quantity:g}")
            qty_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 1, qty_item)
        return table
