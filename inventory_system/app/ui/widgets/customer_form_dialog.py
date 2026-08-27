"""Add/Edit/View customer dialog — same shape as ProductFormDialog/
UserFormDialog: collects raw field values, hands them to SalesService, and
displays whatever it returns or raises (SalesOrderValidationError/
CustomerNotFoundError/DuplicateCustomerCodeError). Client-side checks here
are only the cheap, no-round-trip ones (required name, credit_limit/
opening_balance parse as a non-negative number); everything that needs a
lookup — code uniqueness, email/phone format — is still enforced server-
side and surfaced from whatever SalesService raises, same division of
responsibility as every other form dialog in app.ui.widgets.

Customer code left blank on create is auto-derived from the name by
SalesService, same convention as UserFormDialog's username field.
Status (Active/Inactive) is deliberately not editable here — deactivating/
reactivating goes through the dedicated actions on CustomersPage, same
convention as Users' Activate/Deactivate being separate from the profile-
edit dialog.

read_only=True turns this into a "View Customer" screen: every field
disabled, no Save button — same convention as ProductFormDialog.
"""
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

from app.core.exceptions import (
    CustomerNotFoundError,
    DuplicateCustomerCodeError,
    SalesOrderValidationError,
)
from app.domain.sales import CustomerType
from app.schemas.sales import CustomerCreate, CustomerOut, CustomerUpdate
from app.services.sales_service import SalesService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker


class CustomerFormDialog(QDialog):
    def __init__(self, sales_service: SalesService, customer: CustomerOut | None = None,
                read_only: bool = False, parent=None):
        super().__init__(parent)
        self._sales_service = sales_service
        self._customer = customer
        self.setWindowTitle("View Customer" if read_only else
                            ("Edit Customer" if customer else "Add Customer"))
        constrain_dialog(self, 420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name = QLineEdit(customer.name if customer else "")
        form.addRow("Name *", self._name)

        self._customer_code = QLineEdit(customer.customer_code if customer
                                        and customer.customer_code else "")
        if customer is None:
            self._customer_code.setPlaceholderText("Auto-generated from name if left blank")
        form.addRow("Customer Code", self._customer_code)

        self._customer_type = QComboBox()
        for t in CustomerType:
            self._customer_type.addItem(t.value.title(), t)
        if customer is not None:
            index = self._customer_type.findData(customer.customer_type)
            if index >= 0:
                self._customer_type.setCurrentIndex(index)
        form.addRow("Type *", self._customer_type)

        self._contact_person = QLineEdit(
            customer.contact_person if customer and customer.contact_person else "")
        form.addRow("Contact Person", self._contact_person)

        self._phone = QLineEdit(customer.phone if customer and customer.phone else "")
        form.addRow("Phone", self._phone)

        self._email = QLineEdit(customer.email if customer and customer.email else "")
        form.addRow("Email", self._email)

        self._address = QLineEdit(customer.address if customer and customer.address else "")
        form.addRow("Address", self._address)

        self._city = QLineEdit(customer.city if customer and customer.city else "")
        form.addRow("City", self._city)

        self._state = QLineEdit(customer.state if customer and customer.state else "")
        form.addRow("State/Province", self._state)

        self._country = QLineEdit(customer.country if customer and customer.country else "")
        form.addRow("Country", self._country)

        self._tax_id = QLineEdit(customer.tax_id if customer and customer.tax_id else "")
        form.addRow("Tax/VAT/PAN ID", self._tax_id)

        self._credit_limit = QLineEdit(
            str(customer.credit_limit) if customer and customer.credit_limit is not None else "")
        self._credit_limit.setPlaceholderText("Blank = no credit limit")
        form.addRow("Credit Limit", self._credit_limit)

        self._opening_balance = QLineEdit(
            str(customer.opening_balance) if customer else "0")
        if customer is not None:
            self._opening_balance.setEnabled(False)  # historical, not editable after creation
        form.addRow("Opening Balance", self._opening_balance)

        self._notes = QLineEdit(customer.notes if customer and customer.notes else "")
        form.addRow("Notes", self._notes)

        editable_widgets = [self._name, self._customer_code, self._customer_type,
                            self._contact_person, self._phone, self._email, self._address,
                            self._city, self._state, self._country, self._tax_id,
                            self._credit_limit, self._opening_balance, self._notes]

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

    def _parse_optional_decimal(self, text: str, field_label: str) -> tuple[Decimal | None, str | None]:
        text = text.strip()
        if not text:
            return None, None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None, f"{field_label} must be a number."
        if value < 0:
            return None, f"{field_label} cannot be negative."
        return value, None

    def _submit(self) -> None:
        self._error_label.hide()
        name = self._name.text().strip()
        if not name:
            return self._show_error("Customer name is required.")

        credit_limit, error = self._parse_optional_decimal(self._credit_limit.text(),
                                                            "Credit limit")
        if error:
            return self._show_error(error)
        opening_balance, error = self._parse_optional_decimal(self._opening_balance.text(),
                                                               "Opening balance")
        if error:
            return self._show_error(error)

        customer_code = self._customer_code.text().strip() or None
        contact_person = self._contact_person.text().strip() or None
        phone = self._phone.text().strip() or None
        email = self._email.text().strip() or None
        address = self._address.text().strip() or None
        city = self._city.text().strip() or None
        state = self._state.text().strip() or None
        country = self._country.text().strip() or None
        tax_id = self._tax_id.text().strip() or None
        notes = self._notes.text().strip() or None
        customer_type = self._customer_type.currentData()

        self._set_busy(True)
        if self._customer is None:
            data = CustomerCreate(
                name=name, customer_code=customer_code, customer_type=customer_type,
                contact_person=contact_person, phone=phone, email=email, address=address,
                city=city, state=state, country=country, tax_id=tax_id,
                credit_limit=credit_limit, opening_balance=opening_balance or Decimal("0"),
                notes=notes)
            worker = Worker(self._sales_service.create_customer, data)
        else:
            data = CustomerUpdate(
                name=name, customer_code=customer_code, customer_type=customer_type,
                contact_person=contact_person, phone=phone, email=email, address=address,
                city=city, state=state, country=country, tax_id=tax_id,
                credit_limit=credit_limit, notes=notes)
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
        elif isinstance(exc, (CustomerNotFoundError, DuplicateCustomerCodeError)):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this customer. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
