"""Inventory page — real stock levels via StockService (already fully
working against the excel backend), plus a "+ Stock In" action wired to
InventoryService (the SQL-backed warehouse ledger, see
app/services/inventory_service.py). The two are separate stock-tracking
systems that haven't been merged yet (see docs/architecture.md) — a Stock
In here updates the per-warehouse SQL ledger, not the list below, which is
why a successful Stock In shows its own confirmation (the new on-hand
quantity for that product/warehouse) rather than relying on this table to
reflect it. Only Stock In is wired up; other adjustments/transfers aren't
(inventory.transfer permission exists in the catalog for when that lands).
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThreadPool
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
from app.schemas.product import ProductFilter
from app.schemas.stock import StockLevel
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.services.product_service import ProductService
from app.services.stock_service import StockService
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.stock_in_dialog import StockInDialog
from app.workers.base_worker import Worker

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
        layout.addWidget(PageHeader("Inventory", "Current quantity on hand, per product."))
        layout.addLayout(self._build_toolbar())

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

        self._load_stock_in_options()

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        if self._sessions.is_idle_expired(datetime.now(timezone.utc)):
            return False
        session = self._sessions.peek()
        return session is not None and code in session.permissions

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

    def _load_stock_in_options(self) -> None:
        if not self._can("inventory.adjust"):
            return

        def load():
            products = self._product_service.search_products(
                ProductFilter(status=ProductStatus.ACTIVE, page_size=500))
            warehouses = self._inventory_service.list_warehouses()
            return products.items, warehouses

        worker = Worker(load)
        worker.signals.finished.connect(self._on_stock_in_options_loaded)
        worker.signals.error.connect(self._on_stock_in_options_error)
        QThreadPool.globalInstance().start(worker)

    def _on_stock_in_options_loaded(self, result) -> None:
        self._products, self._warehouses = result

    def _on_stock_in_options_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load products/warehouses for Stock In", exc_info=exc)

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
