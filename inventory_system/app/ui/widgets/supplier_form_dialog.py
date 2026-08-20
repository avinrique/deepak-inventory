"""Add/Edit supplier dialog.

No business logic here — collects raw field values, hands them to
PurchaseService, and displays whatever it returns or raises
(PurchaseOrderValidationError/SupplierNotFoundError). Name validation
lives in app.domain.purchasing / PurchaseService, not in this class — same
convention as ProductFormDialog/WarehouseFormDialog.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from app.core.exceptions import PurchaseOrderValidationError, SupplierNotFoundError
from app.schemas.purchasing import SupplierCreate, SupplierOut, SupplierUpdate
from app.services.purchase_service import PurchaseService
from app.ui.theme import RED, STYLESHEET
from app.workers.base_worker import Worker


class SupplierFormDialog(QDialog):
    def __init__(self, purchase_service: PurchaseService, supplier: SupplierOut | None = None,
                read_only: bool = False, parent=None):
        super().__init__(parent)
        self._purchase_service = purchase_service
        self._supplier = supplier
        self._read_only = read_only
        self.setWindowTitle("View Supplier" if read_only else
                            ("Edit Supplier" if supplier else "Add Supplier"))
        self.setMinimumWidth(420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name = QLineEdit(supplier.name if supplier else "")
        form.addRow("Name *", self._name)

        self._contact_person = QLineEdit(
            supplier.contact_person if supplier and supplier.contact_person else "")
        form.addRow("Contact Person", self._contact_person)

        self._phone = QLineEdit(supplier.phone if supplier and supplier.phone else "")
        form.addRow("Phone", self._phone)

        self._email = QLineEdit(supplier.email if supplier and supplier.email else "")
        form.addRow("Email", self._email)

        self._address = QLineEdit(supplier.address if supplier and supplier.address else "")
        form.addRow("Address", self._address)

        self._tax_id = QLineEdit(supplier.tax_id if supplier and supplier.tax_id else "")
        form.addRow("Tax ID", self._tax_id)

        self._notes = QTextEdit(supplier.notes if supplier and supplier.notes else "")
        self._notes.setFixedHeight(60)
        form.addRow("Notes", self._notes)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        if read_only:
            for widget in (self._name, self._contact_person, self._phone, self._email,
                          self._address, self._tax_id, self._notes):
                widget.setEnabled(False)
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
        else:
            self._save_button = QPushButton("Save Supplier")
            self._save_button.setObjectName("primary")
            self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_button.clicked.connect(self._submit)
            layout.addWidget(self._save_button)

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Supplier")

    def _submit(self) -> None:
        self._error_label.hide()
        name = self._name.text().strip()
        if not name:
            self._show_error("Supplier name is required.")
            return

        contact_person = self._contact_person.text().strip() or None
        phone = self._phone.text().strip() or None
        email = self._email.text().strip() or None
        address = self._address.text().strip() or None
        tax_id = self._tax_id.text().strip() or None
        notes = self._notes.toPlainText().strip() or None

        self._set_busy(True)
        if self._supplier is None:
            data = SupplierCreate(name=name, contact_person=contact_person, phone=phone,
                                  email=email, address=address, tax_id=tax_id, notes=notes)
            worker = Worker(self._purchase_service.create_supplier, data)
        else:
            data = SupplierUpdate(name=name, contact_person=contact_person, phone=phone,
                                  email=email, address=address, tax_id=tax_id, notes=notes)
            worker = Worker(self._purchase_service.update_supplier, self._supplier.id, data)

        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _supplier: SupplierOut) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, PurchaseOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, SupplierNotFoundError):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this supplier. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
