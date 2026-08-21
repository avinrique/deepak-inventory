"""A collapsible "Custom Fields" section — ad-hoc label/value text pairs,
no field-type system, no org-level schema. Shared by New Bill,
SalesOrderFormDialog (both edit the same SalesOrder.custom_fields JSONB
column), and PurchaseOrderFormDialog (PurchaseOrder.custom_fields). Purely
a UI convenience for capturing free-form extra data per bill/order — the
calling form reads get_values() on save and writes it straight into the
custom_fields field of the schema it's building.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.order_form_style import apply_card_shadow


class CustomFieldsSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("formCard")
        apply_card_shadow(self)
        self._rows: list[tuple[QWidget, QLineEdit, QLineEdit]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)

        self._toggle = QToolButton()
        self._toggle.setObjectName("sectionTitle")
        self._toggle.setText("Custom Fields")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setStyleSheet("QToolButton { border: none; background: transparent; }")
        self._toggle.toggled.connect(self._on_toggled)
        outer.addWidget(self._toggle)

        self._content = QWidget()
        self._content.setVisible(False)
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 4, 0, 0)
        content_layout.setSpacing(8)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(6)
        content_layout.addLayout(self._rows_layout)

        add_button = QPushButton("+ Add Field")
        add_button.setObjectName("orderGhost")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(lambda: self._add_row("", ""))
        content_layout.addWidget(add_button, alignment=Qt.AlignmentFlag.AlignLeft)

        outer.addWidget(self._content)

    def _on_toggled(self, checked: bool) -> None:
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._content.setVisible(checked)

    def _add_row(self, key: str, value: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        key_edit = QLineEdit(key)
        key_edit.setPlaceholderText("Field name")
        layout.addWidget(key_edit, stretch=1)

        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("Value")
        layout.addWidget(value_edit, stretch=1)

        remove_button = QPushButton("Remove")
        remove_button.setObjectName("flat")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(lambda: self._remove_row(row))
        layout.addWidget(remove_button)

        self._rows_layout.addWidget(row)
        self._rows.append((row, key_edit, value_edit))

    def _remove_row(self, row: QWidget) -> None:
        self._rows = [entry for entry in self._rows if entry[0] is not row]
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    def get_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for _row, key_edit, value_edit in self._rows:
            key = key_edit.text().strip()
            if not key:
                continue
            values[key] = value_edit.text().strip()
        return values

    def set_values(self, values: dict[str, str] | None) -> None:
        self.clear()
        for key, value in (values or {}).items():
            self._add_row(key, value)
        if values:
            self._toggle.setChecked(True)

    def clear(self) -> None:
        for row, _key_edit, _value_edit in self._rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        self._toggle.setChecked(False)
