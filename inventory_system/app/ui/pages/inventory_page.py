"""Inventory page — live per-warehouse stock levels via InventoryService
(the SQL-backed warehouse ledger, see app/services/inventory_service.py),
plus a "+ Stock In" action that writes to the same ledger. The legacy
Excel-backed StockService (app.services.stock_service) still exists and
still powers the old Tkinter app / billing flow, but this page no longer
reads from it — it was a second, disconnected stock number that a Stock In
here never moved, which made Stock In look broken. Only Stock In is wired
up; other adjustments/transfers aren't (inventory.transfer permission
exists in the catalog for when that lands).
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain.product import ProductStatus
from app.schemas.inventory import InventoryLevel
from app.schemas.product import ProductFilter
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.stock_service import StockService
from app.ui import permission_hints
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.stock_in_dialog import StockInDialog

_logger = logging.getLogger(__name__)


class InventoryPage(QWidget):
    def __init__(self, stock_service: StockService, inventory_service: InventoryService,
                product_service: ProductService, sessions: SessionManager):
        super().__init__()
        self._stock_service = stock_service
        self._inventory_service = inventory_service
        self._product_service = product_service
        self._sessions = sessions
        self._products: list = []
        self._warehouses: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Inventory", "Current quantity on hand, per product "
                                                  "and warehouse."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._load_inventory_data,
            render=self._render_table,
            is_empty=lambda data: len(data[0]) == 0,
            empty_state=EmptyStateWidget(
                "No stock recorded yet", icon="📊",
                message="Stock appears here once you record a Stock In, a purchase "
                        "receipt, or a sale."),
            error_message="Couldn't load inventory levels.")
        layout.addWidget(self._async_area, stretch=1)

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.addStretch()

        if self._can("inventory.adjust"):
            self._stock_in_button = QPushButton("+ Stock In")
            self._stock_in_button.setObjectName("primary")
            self._stock_in_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._stock_in_button.clicked.connect(self._open_stock_in_dialog)
            bar.addWidget(self._stock_in_button)
        return bar

    # -- data flow ------------------------------------------------------#
    def _load_inventory_data(self):
        levels = self._inventory_service.list_all_levels()
        products = self._product_service.search_products(
            ProductFilter(status=ProductStatus.ACTIVE, page_size=500)).items
        warehouses = self._inventory_service.list_warehouses()
        return levels, products, warehouses

    def refresh(self) -> None:
        self._async_area.reload()

    # -- stock in -------------------------------------------------------#
    def _open_stock_in_dialog(self) -> None:
        dialog = StockInDialog(self._inventory_service, self._products, self._warehouses,
                               parent=self)
        if dialog.exec() and dialog.transaction is not None:
            self._show_stock_in_confirmation(dialog.transaction)
            self.refresh()

    def _show_stock_in_confirmation(self, transaction) -> None:
        product = next((p for p in self._products if p.id == transaction.product_id), None)
        warehouse = next((w for w in self._warehouses if w.id == transaction.warehouse_id), None)
        product_label = product.name if product else str(transaction.product_id)
        warehouse_label = warehouse.name if warehouse else str(transaction.warehouse_id)
        QMessageBox.information(
            self, "Stock added",
            f"{product_label} at {warehouse_label} now has "
            f"{transaction.quantity_on_hand_after:g} on hand.")

    # -- table rendering --------------------------------------------- #
    def _render_table(self, data) -> QTableWidget:
        levels, products, warehouses = data
        self._products = products
        self._warehouses = warehouses
        product_names = {p.id: p.name for p in products}
        warehouse_names = {w.id: w.name for w in warehouses}

        table = QTableWidget(len(levels), 4)
        table.setHorizontalHeaderLabels(["Product", "Warehouse", "On Hand", "Available"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setContentsMargins(28, 12, 28, 24)

        def sort_key(level: InventoryLevel) -> str:
            return product_names.get(level.product_id, str(level.product_id)).lower()

        for row, level in enumerate(sorted(levels, key=sort_key)):
            table.setItem(row, 0, QTableWidgetItem(
                product_names.get(level.product_id, str(level.product_id))))
            table.setItem(row, 1, QTableWidgetItem(
                warehouse_names.get(level.warehouse_id, level.warehouse_code)))
            on_hand_item = QTableWidgetItem(f"{level.quantity_on_hand:g}")
            on_hand_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 2, on_hand_item)
            available_item = QTableWidgetItem(f"{level.quantity_available:g}")
            available_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, 3, available_item)
        return table
