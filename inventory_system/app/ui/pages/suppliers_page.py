"""Suppliers page — list/add/edit vendor records via PurchaseService.
Replaces the previous placeholder ("party records aren't typed yet"
reasoning is stale — Supplier has been a real, typed, first-class entity
since app.services.purchase_service/app.repositories.sql.supplier_repository
were built for Purchase Order creation; this page just never got built
alongside them, leaving Purchasing with no way to create a supplier on a
fresh database). No business logic here: every action calls PurchaseService
on a background Worker and renders whatever comes back — validation,
email/phone format checks, and the purchases.*/inventory.view permission
checks all live in the service layer.
"""
import logging

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
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
from app.ui.theme import GREEN_DARK, RED, scale
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.ui.widgets.supplier_form_dialog import SupplierFormDialog
from app.workers.base_worker import Worker

_COLUMNS = ["Supplier", "Contact", "Phone", "Email", "Status", "Actions"]
_ACTIONS_COL = 5
_SEARCH_DEBOUNCE_MS = 300

_logger = logging.getLogger(__name__)


class SuppliersPage(QWidget):
    def __init__(self, purchase_service: PurchaseService, sessions: SessionManager):
        super().__init__()
        self._purchase_service = purchase_service
        self._sessions = sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Suppliers", "Manage vendor relationships."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._load, render=self._render_table,
            is_empty=lambda rows: len(rows) == 0,
            empty_state=EmptyStateWidget(
                "No suppliers yet", icon="🚚",
                message="Try a different search, or add your first supplier."),
            error_message="Couldn't load suppliers.")
        layout.addWidget(self._async_area, stretch=1)

        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self.refresh)

    def refresh(self) -> None:
        self._async_area.reload()

    # -- permissions ------------------------------------------------- #
    def _can(self, code: str) -> bool:
        return permission_hints.can(self._sessions, code)

    # -- toolbar ------------------------------------------------------#
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(28, 4, 28, 12)
        bar.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name, contact, phone, or email…")
        self._search.setFixedWidth(scale(260))
        self._search.textChanged.connect(self._on_search_changed)
        bar.addWidget(self._search)
        bar.addStretch()

        if self._can("purchases.update"):
            add_button = QPushButton("+ Add Supplier")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)
        return bar

    def _on_search_changed(self) -> None:
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)

    # -- data flow ------------------------------------------------------#
    def _load(self) -> list[SupplierOut]:
        suppliers = self._purchase_service.list_suppliers()
        search = self._search.text().strip().lower()
        if not search:
            return suppliers
        return [s for s in suppliers if
               search in s.name.lower()
               or (s.contact_person and search in s.contact_person.lower())
               or (s.phone and search in s.phone.lower())
               or (s.email and search in s.email.lower())]

    # -- table rendering --------------------------------------------- #
    def _render_table(self, rows: list[SupplierOut]) -> QTableWidget:
        table = QTableWidget(len(rows), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for row_idx, supplier in enumerate(rows):
            values = [supplier.name, supplier.contact_person or "—", supplier.phone or "—",
                     supplier.email or "—"]
            for col, value in enumerate(values):
                table.setItem(row_idx, col, QTableWidgetItem(value))

            status_item = QTableWidgetItem("Active" if supplier.is_active else "Inactive")
            status_item.setForeground(QColor(GREEN_DARK if supplier.is_active else RED))
            table.setItem(row_idx, 4, status_item)

            table.setCellWidget(row_idx, _ACTIONS_COL, self._build_actions_button(supplier))
        return table

    def _build_actions_button(self, supplier: SupplierOut) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 0, 8, 0)

        button = QToolButton()
        button.setText("Actions ▾")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setObjectName("rowActions")

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

    # -- dialogs -------------------------------------------------------- #
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

    # -- activate/deactivate ---------------------------------------------- #
    def _activate(self, supplier: SupplierOut) -> None:
        self._run_action(self._purchase_service.update_supplier, supplier.id,
                         SupplierUpdate(is_active=True))

    def _deactivate(self, supplier: SupplierOut) -> None:
        if confirm(self, "Deactivate Supplier",
                  f"Deactivate {supplier.name!r}? It can still be viewed, but won't be "
                  "selectable for new purchase orders going forward.",
                  confirm_label="Deactivate", danger=True):
            self._run_action(self._purchase_service.update_supplier, supplier.id,
                             SupplierUpdate(is_active=False))

    def _run_action(self, fn, *args) -> None:
        worker = Worker(fn, *args)
        worker.signals.finished.connect(lambda _=None: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Supplier action failed", exc_info=exc)
        if isinstance(exc, SupplierNotFoundError):
            QMessageBox.warning(self, "Action Failed", str(exc))
        else:
            QMessageBox.warning(self, "Action Failed", "Something went wrong. Please try again.")
