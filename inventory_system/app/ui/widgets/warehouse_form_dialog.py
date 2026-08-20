"""Add/Edit warehouse dialog.

No business logic here — collects raw field values, hands them to
InventoryService, and displays whatever it returns or raises
(InventoryValidationError/DuplicateWarehouseCodeError/WarehouseNotFoundError).
Code normalization and duplicate checks happen in app.domain.inventory /
InventoryService, not in this class — same convention as ProductFormDialog.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from app.core.exceptions import (
    DuplicateWarehouseCodeError,
    InventoryValidationError,
    WarehouseNotFoundError,
)
from app.schemas.inventory import WarehouseCreate, WarehouseOut, WarehouseUpdate
from app.services.inventory_service import InventoryService
from app.ui.theme import RED, STYLESHEET
from app.workers.base_worker import Worker


class WarehouseFormDialog(QDialog):
    def __init__(self, inventory_service: InventoryService, warehouse: WarehouseOut | None = None,
                read_only: bool = False, parent=None):
        super().__init__(parent)
        self._inventory_service = inventory_service
        self._warehouse = warehouse
        self._read_only = read_only
        self.setWindowTitle("View Warehouse" if read_only else
                            ("Edit Warehouse" if warehouse else "Add Warehouse"))
        self.setMinimumWidth(380)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._code = QLineEdit(warehouse.code if warehouse else "")
        form.addRow("Code *", self._code)

        self._name = QLineEdit(warehouse.name if warehouse else "")
        form.addRow("Name *", self._name)

        self._address = QLineEdit(warehouse.address if warehouse and warehouse.address else "")
        form.addRow("Address", self._address)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        if read_only:
            for widget in (self._code, self._name, self._address):
                widget.setEnabled(False)
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
        else:
            self._save_button = QPushButton("Save Warehouse")
            self._save_button.setObjectName("primary")
            self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_button.clicked.connect(self._submit)
            layout.addWidget(self._save_button)

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Warehouse")

    def _submit(self) -> None:
        self._error_label.hide()
        code = self._code.text().strip()
        name = self._name.text().strip()
        if not code:
            self._show_error("Warehouse code is required.")
            return
        if not name:
            self._show_error("Warehouse name is required.")
            return
        address = self._address.text().strip() or None

        self._set_busy(True)
        if self._warehouse is None:
            data = WarehouseCreate(code=code, name=name, address=address)
            worker = Worker(self._inventory_service.create_warehouse, data)
        else:
            data = WarehouseUpdate(code=code, name=name, address=address)
            worker = Worker(self._inventory_service.update_warehouse, self._warehouse.id, data)

        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _warehouse: WarehouseOut) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, InventoryValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (DuplicateWarehouseCodeError, WarehouseNotFoundError)):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this warehouse. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
