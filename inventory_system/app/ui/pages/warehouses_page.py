"""Warehouses page — list/add/edit/activate/deactivate warehouses.

Replaces the previous placeholder: Warehouse has been a real, typed,
first-class entity (model + SqlWarehouseRepository + InventoryService)
since the inventory ledger/purchasing/sales workflows were built around
warehouse_id — this page just never got built alongside them, which meant
a fresh organization had no way to create its first warehouse through the
UI at all (Purchase/Sales order dialogs need at least one to even enable
their Submit button). No business logic here: every action calls
InventoryService on a background Worker — validation, duplicate-code
checks, and warehouse.manage permission checks all live in the service
layer.
"""
import logging

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import WarehouseNotFoundError
from app.schemas.inventory import WarehouseOut, WarehouseUpdate
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService
from app.ui import permission_hints
from app.ui.theme import GREEN_DARK, MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.warehouse_form_dialog import WarehouseFormDialog
from app.workers.base_worker import Worker

_COLUMNS = ["Code", "Name", "Address", "Status", "Actions"]
_ACTIONS_COL = 4

_logger = logging.getLogger(__name__)


class WarehousesPage(QWidget):
    def __init__(self, inventory_service: InventoryService, sessions: SessionManager):
        super().__init__()
        self._inventory_service = inventory_service
        self._sessions = sessions
        self._current_rows: list[WarehouseOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Warehouses", "Manage storage locations."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._inventory_service.list_warehouses, render=self._render_table,
            is_empty=lambda rows: len(rows) == 0,
            empty_state=EmptyStateWidget(
                "No warehouses yet", icon="🏬",
                message="Add your first warehouse to start receiving stock and "
                       "creating orders."),
            error_message="Couldn't load warehouses.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)
        bar.addStretch()

        if self._can("warehouse.manage"):
            add_button = QPushButton("+ Add Warehouse")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)
        return bar

    def _render_table(self, rows: list[WarehouseOut]) -> QTableWidget:
        self._current_rows = rows

        table = QTableWidget(len(rows), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        for row_idx, w in enumerate(rows):
            values = [w.code, w.name, w.address or "—"]
            for col, value in enumerate(values):
                table.setItem(row_idx, col, QTableWidgetItem(value))

            status_item = QTableWidgetItem("Active" if w.is_active else "Inactive")
            status_item.setForeground(QColor(GREEN_DARK if w.is_active else RED))
            table.setItem(row_idx, 3, status_item)

            table.setCellWidget(row_idx, _ACTIONS_COL, self._build_actions_button(w))
        return table

    def _build_actions_button(self, warehouse: WarehouseOut) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 0, 8, 0)

        button = QToolButton()
        button.setText("Actions ▾")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet(f"border: none; color: {MUTED}; font-size: 12px; padding: 4px 6px;")

        menu = QMenu(button)
        menu.addAction("View").triggered.connect(
            lambda checked=False, w=warehouse: self._open_view(w))
        if self._can("warehouse.manage"):
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, w=warehouse: self._open_edit(w))
            menu.addSeparator()
            if warehouse.is_active:
                menu.addAction("Deactivate").triggered.connect(
                    lambda checked=False, w=warehouse: self._deactivate(w))
            else:
                menu.addAction("Activate").triggered.connect(
                    lambda checked=False, w=warehouse: self._activate(w))

        button.setMenu(menu)
        layout.addWidget(button)
        layout.addStretch()
        return holder

    def _open_add_dialog(self) -> None:
        dialog = WarehouseFormDialog(self._inventory_service, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_view(self, warehouse: WarehouseOut) -> None:
        dialog = WarehouseFormDialog(self._inventory_service, warehouse=warehouse,
                                     read_only=True, parent=self)
        dialog.exec()

    def _open_edit(self, warehouse: WarehouseOut) -> None:
        dialog = WarehouseFormDialog(self._inventory_service, warehouse=warehouse, parent=self)
        if dialog.exec():
            self.refresh()

    def _activate(self, warehouse: WarehouseOut) -> None:
        self._run_action(warehouse.id, WarehouseUpdate(is_active=True))

    def _deactivate(self, warehouse: WarehouseOut) -> None:
        if confirm(self, "Deactivate Warehouse",
                  f"Deactivate {warehouse.name!r}? It will no longer be selectable for new "
                  "orders or stock movements.", confirm_label="Deactivate", danger=True):
            self._run_action(warehouse.id, WarehouseUpdate(is_active=False))

    def _run_action(self, warehouse_id, data: WarehouseUpdate) -> None:
        worker = Worker(self._inventory_service.update_warehouse, warehouse_id, data)
        worker.signals.finished.connect(lambda _=None: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Warehouse action failed", exc_info=exc)
        if isinstance(exc, WarehouseNotFoundError):
            QMessageBox.warning(self, "Action Failed", str(exc))
        else:
            QMessageBox.warning(self, "Action Failed", "Something went wrong. Please try again.")
