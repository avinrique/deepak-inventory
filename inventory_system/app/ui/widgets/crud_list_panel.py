"""Generic add/rename/delete list-with-form panel — Category, Brand, and
Unit management (see catalog_manager_dialog.py) are the same "CRUD on a
list of named things" shape three times, so this is one reusable widget
instead of three near-identical copies. No business logic here: it collects
field text, builds the given Pydantic schema, and calls whatever
create/update/delete/list callables it was given — CatalogService owns the
actual rules (permissions, persistence).
"""
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import RED
from app.ui.widgets.confirm_dialog import confirm
from app.workers.base_worker import Worker


class CrudListPanel(QWidget):
    def __init__(self, *, fields: list[tuple[str, str]], optional_fields: set[str],
                display: Callable[[Any], str], schema_cls: type, list_fn, create_fn,
                update_fn, delete_fn):
        super().__init__()
        self._fields = fields
        self._optional_fields = optional_fields
        self._display = display
        self._schema_cls = schema_cls
        self._list_fn = list_fn
        self._create_fn = create_fn
        self._update_fn = update_fn
        self._delete_fn = delete_fn
        self._items: list = []
        self._selected_id = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, stretch=1)

        form_box = QVBoxLayout()
        form = QFormLayout()
        self._inputs: dict[str, QLineEdit] = {}
        for label, attr in fields:
            field = QLineEdit()
            form.addRow(label, field)
            self._inputs[attr] = field
        form_box.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        form_box.addWidget(self._error_label)

        button_row = QHBoxLayout()
        self._save_button = QPushButton("Add")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.clicked.connect(self._save)
        button_row.addWidget(self._save_button)

        self._delete_button = QPushButton("Delete")
        self._delete_button.setObjectName("danger")
        self._delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._delete)
        button_row.addWidget(self._delete_button)

        new_button = QPushButton("New")
        new_button.setObjectName("flat")
        new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        new_button.clicked.connect(self._clear_form)
        button_row.addWidget(new_button)

        form_box.addLayout(button_row)
        form_box.addStretch()
        layout.addLayout(form_box, stretch=1)

        self.reload()

    def reload(self) -> None:
        worker = Worker(self._list_fn)
        worker.signals.finished.connect(self._on_loaded)
        worker.signals.error.connect(lambda exc: self._show_error("Couldn't load the list."))
        QThreadPool.globalInstance().start(worker)

    def _on_loaded(self, items: list) -> None:
        self._items = items
        self._list.clear()
        for item in items:
            list_item = QListWidgetItem(self._display(item))
            list_item.setData(Qt.ItemDataRole.UserRole, item.id)
            self._list.addItem(list_item)

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            return
        item = self._items[row]
        self._selected_id = item.id
        for _, attr in self._fields:
            self._inputs[attr].setText(getattr(item, attr) or "")
        self._save_button.setText("Update")
        self._delete_button.setEnabled(True)

    def _clear_form(self) -> None:
        self._selected_id = None
        for field in self._inputs.values():
            field.clear()
        self._save_button.setText("Add")
        self._delete_button.setEnabled(False)
        self._list.setCurrentRow(-1)
        self._error_label.hide()

    def _save(self) -> None:
        self._error_label.hide()
        values: dict[str, str | None] = {}
        for label, attr in self._fields:
            text = self._inputs[attr].text().strip()
            if not text and attr not in self._optional_fields:
                self._show_error(f"{label} is required.")
                return
            values[attr] = text or None

        data = self._schema_cls(**values)
        self._save_button.setEnabled(False)
        if self._selected_id is None:
            worker = Worker(self._create_fn, data)
        else:
            worker = Worker(self._update_fn, self._selected_id, data)
        worker.signals.finished.connect(self._on_saved)
        worker.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(worker)

    def _on_saved(self, _result) -> None:
        self._save_button.setEnabled(True)
        self._clear_form()
        self.reload()

    def _on_save_error(self, exc: Exception) -> None:
        self._save_button.setEnabled(True)
        self._show_error("Couldn't save — that name may already be in use.")

    def _delete(self) -> None:
        if self._selected_id is None:
            return
        if not confirm(self, "Delete", "Delete this item? This cannot be undone.",
                       danger=True):
            return
        target_id = self._selected_id
        worker = Worker(self._delete_fn, target_id)
        worker.signals.finished.connect(lambda _: (self._clear_form(), self.reload()))
        worker.signals.error.connect(lambda exc: self._show_error(
            "Couldn't delete — it's probably in use by a product."))
        QThreadPool.globalInstance().start(worker)

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
