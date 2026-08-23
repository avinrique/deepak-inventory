"""A clearable From/To date-range filter for the transaction list pages.

Mirrors the "checkbox enables a QDateEdit" convention already used for
Purchase Order's Expected Date and Sales Order's Delivery Date
(app/ui/widgets/purchase_order_form_dialog.py,
app/ui/widgets/sales_order_form_dialog.py) — unchecked means "no bound",
not "today", so the filter defaults to showing everything.
"""
from datetime import date

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import QCheckBox, QDateEdit, QHBoxLayout, QLabel, QWidget


class DateRangeFilter(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(QLabel("From"))
        self._from_check = QCheckBox()
        self._from_edit = QDateEdit(QDate.currentDate())
        self._from_edit.setCalendarPopup(True)
        self._from_edit.setEnabled(False)
        self._from_check.toggled.connect(self._from_edit.setEnabled)
        self._from_check.toggled.connect(self.changed)
        self._from_edit.dateChanged.connect(self._emit_if_active)
        layout.addWidget(self._from_check)
        layout.addWidget(self._from_edit)

        layout.addWidget(QLabel("To"))
        self._to_check = QCheckBox()
        self._to_edit = QDateEdit(QDate.currentDate())
        self._to_edit.setCalendarPopup(True)
        self._to_edit.setEnabled(False)
        self._to_check.toggled.connect(self._to_edit.setEnabled)
        self._to_check.toggled.connect(self.changed)
        self._to_edit.dateChanged.connect(self._emit_if_active)
        layout.addWidget(self._to_check)
        layout.addWidget(self._to_edit)

    def _emit_if_active(self) -> None:
        # A date picker fires dateChanged even while its checkbox is off
        # (Qt keeps a value on a disabled QDateEdit) — only a change to an
        # ACTIVE bound should trigger a re-filter.
        if self.sender() is self._from_edit and not self._from_check.isChecked():
            return
        if self.sender() is self._to_edit and not self._to_check.isChecked():
            return
        self.changed.emit()

    def date_from(self) -> date | None:
        return self._from_edit.date().toPython() if self._from_check.isChecked() else None

    def date_to(self) -> date | None:
        return self._to_edit.date().toPython() if self._to_check.isChecked() else None

    def clear(self) -> None:
        self._from_check.setChecked(False)
        self._to_check.setChecked(False)
