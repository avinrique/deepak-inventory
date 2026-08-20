"""Suppliers page — list/add/edit/activate/deactivate suppliers.

Replaces the previous placeholder: Supplier has been a real, typed,
first-class entity (model + SqlSupplierRepository + PurchaseService) since
the purchasing workflow was built around supplier_id — this page just
never got built alongside them, which meant a fresh organization had no
way to create its first supplier through the UI at all (the Purchase
Order dialog needs at least one to even enable its Submit button). No
business logic here: every action calls PurchaseService on a background
Worker — validation and purchases.* permission checks all live in the
service layer.
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

from app.core.exceptions import SupplierNotFoundError
from app.schemas.purchasing import SupplierOut, SupplierUpdate
from app.security.session import SessionManager
from app.services.purchase_service import PurchaseService
from app.ui import permission_hints
from app.ui.theme import GREEN_DARK, MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.supplier_form_dialog import SupplierFormDialog
from app.workers.base_worker import Worker

_COLUMNS = ["Name", "Contact Person", "Phone", "Email", "Status", "Actions"]
_ACTIONS_COL = 5

_logger = logging.getLogger(__name__)


class SuppliersPage(QWidget):
    def __init__(self, purchase_service: PurchaseService, sessions: SessionManager):
        super().__init__()
        self._purchase_service = purchase_service
        self._sessions = sessions
        self._current_rows: list[SupplierOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Suppliers", "Manage vendor relationships."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._purchase_service.list_suppliers, render=self._render_table,
            is_empty=lambda rows: len(rows) == 0,
            empty_state=EmptyStateWidget(
                "No suppliers yet", icon="🚚",
                message="Add your first supplier to start creating purchase orders."),
            error_message="Couldn't load suppliers.")
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

        if self._can("purchases.update"):
            add_button = QPushButton("+ Add Supplier")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)
        return bar

    def _render_table(self, rows: list[SupplierOut]) -> QTableWidget:
        self._current_rows = rows

        table = QTableWidget(len(rows), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for row_idx, s in enumerate(rows):
            values = [s.name, s.contact_person or "—", s.phone or "—", s.email or "—"]
            for col, value in enumerate(values):
                table.setItem(row_idx, col, QTableWidgetItem(value))

            status_item = QTableWidgetItem("Active" if s.is_active else "Inactive")
            status_item.setForeground(QColor(GREEN_DARK if s.is_active else RED))
            table.setItem(row_idx, 4, status_item)

            table.setCellWidget(row_idx, _ACTIONS_COL, self._build_actions_button(s))
        return table

    def _build_actions_button(self, supplier: SupplierOut) -> QWidget:
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
            lambda checked=False, s=supplier: self._open_view(s))
        if self._can("purchases.update"):
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, s=supplier: self._open_edit(s))
            menu.addSeparator()
            if supplier.is_active:
                menu.addAction("Deactivate").triggered.connect(
                    lambda checked=False, s=supplier: self._deactivate(s))
            else:
                menu.addAction("Activate").triggered.connect(
                    lambda checked=False, s=supplier: self._activate(s))

        button.setMenu(menu)
        layout.addWidget(button)
        layout.addStretch()
        return holder

    def _open_add_dialog(self) -> None:
        dialog = SupplierFormDialog(self._purchase_service, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_view(self, supplier: SupplierOut) -> None:
        dialog = SupplierFormDialog(self._purchase_service, supplier=supplier, read_only=True,
                                    parent=self)
        dialog.exec()

    def _open_edit(self, supplier: SupplierOut) -> None:
        dialog = SupplierFormDialog(self._purchase_service, supplier=supplier, parent=self)
        if dialog.exec():
            self.refresh()

    def _activate(self, supplier: SupplierOut) -> None:
        self._run_action(supplier.id, SupplierUpdate(is_active=True))

    def _deactivate(self, supplier: SupplierOut) -> None:
        if confirm(self, "Deactivate Supplier",
                  f"Deactivate {supplier.name!r}? It will no longer be selectable for new "
                  "purchase orders.", confirm_label="Deactivate", danger=True):
            self._run_action(supplier.id, SupplierUpdate(is_active=False))

    def _run_action(self, supplier_id, data: SupplierUpdate) -> None:
        worker = Worker(self._purchase_service.update_supplier, supplier_id, data)
        worker.signals.finished.connect(lambda _=None: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Supplier action failed", exc_info=exc)
        if isinstance(exc, SupplierNotFoundError):
            QMessageBox.warning(self, "Action Failed", str(exc))
        else:
            QMessageBox.warning(self, "Action Failed", "Something went wrong. Please try again.")
