"""Breadcrumb trail shown in the header — set_path() re-renders it."""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.ui.theme import MUTED, TEXT


class BreadcrumbBar(QWidget):
    def __init__(self):
        super().__init__()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.set_path(["Home"])

    def set_path(self, parts: list[str]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            label = QLabel(part)
            weight = 700 if is_last else 500
            color = TEXT if is_last else MUTED
            label.setStyleSheet(f"font-size: 13px; font-weight: {weight}; color: {color};")
            self._layout.addWidget(label)
            if not is_last:
                sep = QLabel("›")
                sep.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
                self._layout.addWidget(sep)
