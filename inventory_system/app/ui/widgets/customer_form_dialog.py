"""Add/Edit/View customer dialog — same shape as ProductFormDialog/
UserFormDialog: collects raw field values, hands them to SalesService, and
displays whatever it returns or raises (SalesOrderValidationError/
CustomerNotFoundError). Name-required validation is the only rule
(app.domain.sales.validate_customer) — everything else is optional contact
detail, so there's nothing else to check client-side.

Status (Active/Inactive) is deliberately not editable here — deactivating/
reactivating goes through the dedicated actions on CustomersPage (calling
update_customer with only is_active set), same convention as Users'
Activate/Deactivate being separate from the profile-edit dialog.

read_only=True turns this into a "View Customer" screen: every field
disabled, no Save button — same convention as ProductFormDialog.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from app.core.exceptions import CustomerNotFoundError, SalesOrderValidationError
from app.schemas.sales import CustomerCreate, CustomerOut, CustomerUpdate
from app.services.sales_service import SalesService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.workers.base_worker import Worker


class CustomerFormDialog(QDialog):
    def __init__(self, sales_service: SalesService, customer: CustomerOut | None = None,
                read_only: bool = False, parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._customer = customer
        self.setWindowTitle("View Customer" if read_only else
                            ("Edit Customer" if customer else "Add Customer"))
        self.setMinimumWidth(400)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name = QLineEdit(customer.name if customer else "")
        form.addRow("Name *", self._name)

        self._contact_person = QLineEdit(
            customer.contact_person if customer and customer.contact_person else "")
        form.addRow("Contact Person", self._contact_person)

        self._phone = QLineEdit(customer.phone if customer and customer.phone else "")
        form.addRow("Phone", self._phone)

        self._email = QLineEdit(customer.email if customer and customer.email else "")
        form.addRow("Email", self._email)

        self._address = QLineEdit(customer.address if customer and customer.address else "")
        form.addRow("Address", self._address)

        self._tax_id = QLineEdit(customer.tax_id if customer and customer.tax_id else "")
        form.addRow("Tax ID", self._tax_id)

        self._notes = QLineEdit(customer.notes if customer and customer.notes else "")
        form.addRow("Notes", self._notes)

        editable_widgets = [self._name, self._contact_person, self._phone, self._email,
                            self._address, self._tax_id, self._notes]

        if customer is not None:
            status_label = QLabel("Active" if customer.is_active else "Inactive")
            status_label.setStyleSheet(f"color: {MUTED};")
            form.addRow("Status", status_label)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        if read_only:
            for widget in editable_widgets:
                widget.setEnabled(False)
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
        else:
            self._save_button = QPushButton("Save Customer")
            self._save_button.setObjectName("primary")
            self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_button.clicked.connect(self._submit)
            layout.addWidget(self._save_button)

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Customer")

    def _submit(self) -> None:
        self._error_label.hide()
        name = self._name.text().strip()
        if not name:
            return self._show_error("Customer name is required.")

        contact_person = self._contact_person.text().strip() or None
        phone = self._phone.text().strip() or None
        email = self._email.text().strip() or None
        address = self._address.text().strip() or None
        tax_id = self._tax_id.text().strip() or None
        notes = self._notes.text().strip() or None

        self._set_busy(True)
        if self._customer is None:
            data = CustomerCreate(name=name, contact_person=contact_person, phone=phone,
                                  email=email, address=address, tax_id=tax_id, notes=notes)
            worker = Worker(self._sales_service.create_customer, data)
        else:
            data = CustomerUpdate(name=name, contact_person=contact_person, phone=phone,
                                  email=email, address=address, tax_id=tax_id, notes=notes)
            worker = Worker(self._sales_service.update_customer, self._customer.id, data)

        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _customer: CustomerOut) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, SalesOrderValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, CustomerNotFoundError):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this customer. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
