"""Customers page — search/filter/sort/paginate customer records, add/edit/
view, activate/deactivate, transaction history, CSV export. Replaces the
previous placeholder (the "party records aren't typed yet" reasoning it
gave is stale — Customer has been a real, typed, first-class entity since
app.services.sales_service/app.repositories.sql.customer_repository were
built for Sales Order creation; this page just never got built alongside
them). No business logic here: every action calls SalesService on a
background Worker and renders whatever comes back — validation, duplicate-
code checks, credit control, and the customers.* permission checks all
live in the service layer.

Outstanding/Credit Limit are fetched one SalesService.get_customer_balance
call per row in the background load — same "organizations are small
enough for a client-side pass over one list" assumption UsersPage already
makes for its own unpaginated list, not a new one.
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
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

from app.core.exceptions import CustomerNotFoundError
from app.domain.sales import CustomerType
from app.schemas.reporting import ReportResult
from app.schemas.sales import CustomerOut
from app.security.session import SessionManager
from app.services.sales_service import SalesService
from app.ui import permission_hints
from app.ui.theme import GREEN_DARK, MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.customer_form_dialog import CustomerFormDialog
from app.ui.widgets.customer_history_dialog import CustomerHistoryDialog
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_COLUMNS = ["Customer", "Code", "Phone", "Email", "Type", "Outstanding", "Credit Limit",
           "Status", "Actions"]
_SEARCH_DEBOUNCE_MS = 300
_ACTIONS_COL = 8

_logger = logging.getLogger(__name__)


def _money(value) -> str:
    return f"{value:,.2f}" if value is not None else "—"


class _Row:
    __slots__ = ("customer", "outstanding", "credit_limit")

    def __init__(self, customer, outstanding, credit_limit):
        self.customer = customer
        self.outstanding = outstanding
        self.credit_limit = credit_limit


class CustomersPage(QWidget):
    def __init__(self, sales_service: SalesService, sessions: SessionManager):
        super().__init__()
        self._sales_service = sales_service
        self._sessions = sessions
        self._current_rows: list[_Row] = []
        self._sort_by = "name"
        self._sort_desc = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Customers", "Manage customer accounts, credit, and "
                                                  "transaction history."))
        layout.addLayout(self._build_toolbar())

        self._async_area = AsyncContentArea(
            load=self._load, render=self._render_table,
            is_empty=lambda rows: len(rows) == 0,
            empty_state=EmptyStateWidget(
                "No customers found", icon="👥",
                message="Try a different search or filter, or add your first customer."),
            error_message="Couldn't load customers.")
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
        self._search.setPlaceholderText("Search name, code, phone, or email…")
        self._search.setFixedWidth(240)
        self._search.textChanged.connect(self._on_search_changed)
        bar.addWidget(self._search)

        self._type_filter = QComboBox()
        self._type_filter.addItem("All Types", None)
        for t in CustomerType:
            self._type_filter.addItem(t.value.title(), t)
        self._type_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._type_filter)

        self._status_filter = QComboBox()
        self._status_filter.addItem("All Statuses", None)
        self._status_filter.addItem("Active", True)
        self._status_filter.addItem("Inactive", False)
        self._status_filter.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._status_filter)

        bar.addStretch()

        if self._can("customers.export"):
            export_button = QPushButton("Export CSV")
            export_button.setObjectName("ghost")
            export_button.setCursor(Qt.CursorShape.PointingHandCursor)
            export_button.clicked.connect(self._export_csv)
            bar.addWidget(export_button)

        if self._can("customers.create"):
            add_button = QPushButton("+ Add Customer")
            add_button.setObjectName("primary")
            add_button.setCursor(Qt.CursorShape.PointingHandCursor)
            add_button.clicked.connect(self._open_add_dialog)
            bar.addWidget(add_button)

        return bar

    # -- data flow ------------------------------------------------------#
    def _on_search_changed(self) -> None:
        self._search_debounce.start(_SEARCH_DEBOUNCE_MS)

    def _on_filter_changed(self) -> None:
        self.refresh()

    def _on_sort_clicked(self, column: int) -> None:
        sort_keys = {0: "name", 1: "customer_code", 4: "customer_type"}
        key = sort_keys.get(column)
        if key is None:
            return
        if self._sort_by == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_by = key
            self._sort_desc = False
        self.refresh()

    def _load(self) -> list[_Row]:
        customers = self._sales_service.list_customers()

        search = self._search.text().strip().lower()
        type_filter = self._type_filter.currentData()
        status_filter = self._status_filter.currentData()

        filtered = customers
        if search:
            filtered = [c for c in filtered if
                       search in c.name.lower()
                       or (c.customer_code and search in c.customer_code.lower())
                       or (c.phone and search in c.phone.lower())
                       or (c.email and search in c.email.lower())]
        if type_filter is not None:
            filtered = [c for c in filtered if c.customer_type == type_filter]
        if status_filter is not None:
            filtered = [c for c in filtered if c.is_active == status_filter]

        def sort_key(c: CustomerOut):
            value = getattr(c, self._sort_by)
            if value is None:
                return (0, "")
            if hasattr(value, "value"):  # enum
                value = value.value
            return (1, value.lower() if isinstance(value, str) else value)

        filtered = sorted(filtered, key=sort_key, reverse=self._sort_desc)

        rows = []
        for customer in filtered:
            outstanding = None
            if self._can("customers.view"):
                try:
                    balance = self._sales_service.get_customer_balance(customer.id)
                    outstanding = balance.outstanding_balance
                except CustomerNotFoundError:
                    outstanding = None
            rows.append(_Row(customer=customer, outstanding=outstanding,
                            credit_limit=customer.credit_limit))
        return rows

    # -- table rendering --------------------------------------------- #
    def _render_table(self, rows: list[_Row]) -> QTableWidget:
        self._current_rows = rows

        table = QTableWidget(len(rows), len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().sectionClicked.connect(self._on_sort_clicked)

        right_align = {5, 6}
        for row_idx, row in enumerate(rows):
            c = row.customer
            values = [c.name, c.customer_code or "—", c.phone or "—", c.email or "—",
                     c.customer_type.value.title(), _money(row.outstanding),
                     _money(row.credit_limit) if row.credit_limit is not None else "No limit"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in right_align:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                          | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_idx, col, item)

            status_item = QTableWidgetItem("Active" if c.is_active else "Inactive")
            status_item.setForeground(QColor(GREEN_DARK if c.is_active else RED))
            table.setItem(row_idx, 7, status_item)

            table.setCellWidget(row_idx, _ACTIONS_COL, self._build_actions_button(c))
        return table

    def _build_actions_button(self, customer: CustomerOut) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 0, 8, 0)

        button = QToolButton()
        button.setText("Actions ▾")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet(f"border: none; color: {MUTED}; font-size: 12px; padding: 4px 6px;")

        menu = QMenu(button)
        if self._can("customers.view"):
            menu.addAction("View").triggered.connect(
                lambda checked=False, c=customer: self._open_view(c))
            menu.addAction("Transaction History").triggered.connect(
                lambda checked=False, c=customer: self._open_history(c))
        if self._can("customers.update"):
            menu.addAction("Edit").triggered.connect(
                lambda checked=False, c=customer: self._open_edit(c))
        if self._can("customers.deactivate"):
            menu.addSeparator()
            if customer.is_active:
                menu.addAction("Deactivate").triggered.connect(
                    lambda checked=False, c=customer: self._deactivate(c))
            else:
                menu.addAction("Activate").triggered.connect(
                    lambda checked=False, c=customer: self._activate(c))

        if menu.isEmpty():
            button.setEnabled(False)
        button.setMenu(menu)
        layout.addWidget(button)
        layout.addStretch()
        return holder

    # -- dialogs -------------------------------------------------------- #
    def _open_add_dialog(self) -> None:
        dialog = CustomerFormDialog(self._sales_service, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_view(self, customer: CustomerOut) -> None:
        dialog = CustomerFormDialog(self._sales_service, customer=customer, read_only=True,
                                    parent=self)
        dialog.exec()

    def _open_edit(self, customer: CustomerOut) -> None:
        dialog = CustomerFormDialog(self._sales_service, customer=customer, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_history(self, customer: CustomerOut) -> None:
        dialog = CustomerHistoryDialog(self._sales_service, customer, parent=self)
        dialog.exec()

    # -- activate/deactivate ---------------------------------------------- #
    def _activate(self, customer: CustomerOut) -> None:
        self._run_action(self._sales_service.activate_customer, customer.id)

    def _deactivate(self, customer: CustomerOut) -> None:
        if confirm(self, "Deactivate Customer",
                  f"Deactivate {customer.name!r}? They can still be viewed, but "
                  "won't be selectable for new sales orders' filters going forward.",
                  confirm_label="Deactivate", danger=True):
            self._run_action(self._sales_service.deactivate_customer, customer.id)

    def _run_action(self, fn, *args) -> None:
        worker = Worker(fn, *args)
        worker.signals.finished.connect(lambda _=None: self.refresh())
        worker.signals.error.connect(self._on_action_error)
        QThreadPool.globalInstance().start(worker)

    def _on_action_error(self, exc: Exception) -> None:
        _logger.exception("Customer action failed", exc_info=exc)
        if isinstance(exc, CustomerNotFoundError):
            QMessageBox.warning(self, "Action Failed", str(exc))
        else:
            QMessageBox.warning(self, "Action Failed", "Something went wrong. Please try again.")

    # -- export ------------------------------------------------------------#
    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Customers", "customers.csv",
                                              "CSV Files (*.csv)")
        if not path:
            return
        worker = Worker(self._sales_service.export_customers)
        worker.signals.finished.connect(lambda customers: self._on_export_loaded(customers, path))
        worker.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(worker)

    def _on_export_loaded(self, customers: list[CustomerOut], path: str) -> None:
        from app.reporting.export import export_csv
        rows = [{"Name": c.name, "Code": c.customer_code, "Type": c.customer_type.value,
                "Phone": c.phone, "Email": c.email, "Credit Limit": c.credit_limit,
                "Opening Balance": c.opening_balance,
                "Status": "Active" if c.is_active else "Inactive"} for c in customers]
        result = ReportResult(title="Customers", generated_at=datetime.now(timezone.utc),
                              columns=["Name", "Code", "Type", "Phone", "Email", "Credit Limit",
                                      "Opening Balance", "Status"], rows=rows)
        try:
            export_csv(result, path)
            QMessageBox.information(self, "Export Complete", f"Customers exported to {path}")
        except OSError as exc:
            _logger.exception("Customer CSV export failed", exc_info=exc)
            QMessageBox.warning(self, "Export Failed", "Couldn't write the export file.")

    def _on_export_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load customers for export", exc_info=exc)
        QMessageBox.warning(self, "Export Failed", "Couldn't load customers to export.")
