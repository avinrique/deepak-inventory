"""Record a payment against an already-generated invoice. Loads the
invoice's current balance (SalesService.get_invoice_document) before
showing the form, so the amount field can default to — and the summary
can show — what's actually still owed, rather than asking the user to go
look that up themselves first.

No business logic here: the outstanding-balance check, row locking, and
auto-completing the order once fully paid all live in
SalesOrderRepository.record_payment (see its docstring) — this dialog only
collects amount/method/notes and displays whatever comes back.
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.exceptions import InvoiceNotFoundError, OverpaymentError, SalesOrderValidationError
from app.domain.sales import PaymentMethod
from app.schemas.sales import InvoiceDocumentData, PaymentRequest
from app.services.sales_service import SalesService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class RecordPaymentDialog(QDialog):
    def __init__(self, sales_service: SalesService, invoice_id: uuid.UUID, parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._invoice_id = invoice_id
        self._amount_due: Decimal | None = None
        self.setWindowTitle("Record Payment")
        self.setMinimumWidth(380)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        self._summary_label = QLabel("Loading invoice…")
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet(f"color: {MUTED}; font-size: 12.5px;")
        layout.addWidget(self._summary_label)

        form = QFormLayout()
        form.setSpacing(8)

        self._amount = QLineEdit()
        self._amount.setEnabled(False)
        form.addRow("Amount *", self._amount)

        self._method = QComboBox()
        for method in PaymentMethod:
            self._method.addItem(method.value.replace("_", " ").title(), method)
        form.addRow("Method *", self._method)

        self._notes = QLineEdit()
        self._notes.setPlaceholderText("Optional")
        form.addRow("Notes", self._notes)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._save_button = QPushButton("Record Payment")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._submit)
        layout.addWidget(self._save_button)

        self._load()

    def _load(self) -> None:
        worker = Worker(self._sales_service.get_invoice_document, self._invoice_id)
        worker.signals.finished.connect(self._on_loaded)
        worker.signals.error.connect(self._on_load_error)
        QThreadPool.globalInstance().start(worker)

    def _on_loaded(self, doc: InvoiceDocumentData) -> None:
        self._amount_due = doc.amount_due
        self._summary_label.setText(
            f"Invoice {doc.invoice_number} — total {doc.total:,.2f}, "
            f"paid {doc.amount_paid:,.2f}, due {doc.amount_due:,.2f}")
        self._amount.setText(f"{doc.amount_due:.2f}")
        self._amount.setEnabled(True)
        self._save_button.setEnabled(True)

    def _on_load_error(self, exc: Exception) -> None:
        _logger.exception("Failed to load invoice for payment", exc_info=exc)
        if isinstance(exc, InvoiceNotFoundError):
            self._summary_label.setText(str(exc))
        else:
            self._summary_label.setText("Couldn't load this invoice. Please try again.")

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Record Payment")

    def _submit(self) -> None:
        self._error_label.hide()
        try:
            amount = Decimal(self._amount.text().strip() or "0")
        except InvalidOperation:
            self._show_error("Amount must be a number.")
            return
        if amount <= 0:
            self._show_error("Amount must be greater than zero.")
            return

        data = PaymentRequest(invoice_id=self._invoice_id, amount=amount,
                              method=self._method.currentData(),
                              notes=self._notes.text().strip() or None)
        self._set_busy(True)
        worker = Worker(self._sales_service.record_payment, data)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _payment) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, OverpaymentError):
            self._show_error(str(exc))
        elif isinstance(exc, SalesOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, InvoiceNotFoundError):
            self._show_error(str(exc))
        else:
            _logger.exception("Recording payment failed", exc_info=exc)
            self._show_error("Something went wrong recording this payment. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
