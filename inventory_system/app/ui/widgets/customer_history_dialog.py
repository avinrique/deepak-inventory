"""Customer profile / transaction history dialog — current balance, credit
limit, available credit, and a merged timeline of sales orders, invoices,
payments, and returns for one customer. Read-only: no business logic, no
writes. Every figure comes from SalesService.get_customer_balance/
get_customer_history, which in turn only aggregate what Billing
(SalesOrderRepository) already recorded — nothing here recomputes a price,
tax, or total.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.schemas.sales import CustomerOut
from app.services.sales_service import SalesService
from app.ui.theme import GREEN_DARK, MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import fit_to_screen
from app.workers.base_worker import Worker

_KIND_LABELS = {"sales_order": "Sales Order", "invoice": "Invoice", "payment": "Payment",
               "return": "Return"}


def _money(value) -> str:
    return f"{value:,.2f}" if value is not None else "—"


class CustomerHistoryDialog(QDialog):
    def __init__(self, sales_service: SalesService, customer: CustomerOut, parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._customer = customer
        self.setWindowTitle(f"Customer History — {customer.name}")
        fit_to_screen(self, 640, 520, minimum_width=460, minimum_height=340)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header = QLabel(customer.name)
        header.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(header)

        self._summary_row = QHBoxLayout()
        self._summary_row.setSpacing(24)
        self._outstanding_label = self._stat_label("Outstanding", "…")
        self._credit_limit_label = self._stat_label("Credit Limit", "…")
        self._available_label = self._stat_label("Available Credit", "…")
        for w in (self._outstanding_label, self._credit_limit_label, self._available_label):
            self._summary_row.addWidget(w)
        self._summary_row.addStretch()
        layout.addLayout(self._summary_row)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Type", "Reference", "Amount", "Date"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, stretch=1)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._status_label)

        close_button = QPushButton("Close")
        close_button.setObjectName("primary")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self._load()

    @staticmethod
    def _stat_label(title: str, value: str) -> QLabel:
        label = QLabel(f"{title}\n{value}")
        label.setStyleSheet("font-size: 13px;")
        return label

    def _load(self) -> None:
        self._status_label.setText("Loading…")

        def load():
            balance = self._sales_service.get_customer_balance(self._customer.id)
            history = self._sales_service.get_customer_history(self._customer.id)
            return balance, history

        worker = Worker(load)
        worker.signals.finished.connect(self._on_loaded)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_loaded(self, result) -> None:
        balance, history = result
        self._status_label.setText("")

        outstanding_color = RED if balance.outstanding_balance > 0 else GREEN_DARK
        self._outstanding_label.setText(f"Outstanding\n{_money(balance.outstanding_balance)}")
        self._outstanding_label.setStyleSheet(
            f"font-size: 13px; color: {outstanding_color}; font-weight: 700;")
        self._credit_limit_label.setText(
            f"Credit Limit\n{_money(balance.credit_limit) if balance.credit_limit is not None else 'No limit'}")
        available = balance.available_credit
        self._available_label.setText(
            f"Available Credit\n{_money(available) if available is not None else '—'}")

        self._table.setRowCount(len(history))
        for row, txn in enumerate(history):
            self._table.setItem(row, 0, QTableWidgetItem(_KIND_LABELS.get(txn.kind, txn.kind)))
            self._table.setItem(row, 1, QTableWidgetItem(txn.reference))
            amount_item = QTableWidgetItem(_money(txn.amount))
            amount_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                         | Qt.AlignmentFlag.AlignVCenter)
            self._table.setItem(row, 2, amount_item)
            self._table.setItem(row, 3, QTableWidgetItem(txn.occurred_at.strftime("%Y-%m-%d %H:%M")))

        if not history:
            self._status_label.setText("No transactions recorded for this customer yet.")

    def _on_error(self, exc: Exception) -> None:
        self._status_label.setText("Couldn't load customer history.")
