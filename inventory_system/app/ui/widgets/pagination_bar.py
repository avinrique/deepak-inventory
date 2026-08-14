"""Reusable pagination control: Prev/Next + "Page X of Y (N total)". Emits
page_changed(new_page) — the caller (a page's AsyncContentArea reload) owns
what happens next; this widget has no idea what it's paginating.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from app.ui.theme import MUTED


class PaginationBar(QWidget):
    page_changed = Signal(int)

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 10, 28, 16)
        layout.setSpacing(10)

        self._prev_button = QPushButton("‹ Prev")
        self._prev_button.setObjectName("flat")
        self._prev_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_button.clicked.connect(self._go_prev)
        layout.addWidget(self._prev_button)

        self._label = QLabel("")
        self._label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._label)

        self._next_button = QPushButton("Next ›")
        self._next_button.setObjectName("flat")
        self._next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_button.clicked.connect(self._go_next)
        layout.addWidget(self._next_button)
        layout.addStretch()

        self._page = 1
        self._total_pages = 1
        self._set_visible_state(total=0)

    def set_state(self, *, page: int, total_pages: int, total: int) -> None:
        self._page = page
        self._total_pages = total_pages
        self._set_visible_state(total=total)

    def _set_visible_state(self, total: int) -> None:
        self._label.setText(f"Page {self._page} of {self._total_pages}  ·  {total} total")
        self._prev_button.setEnabled(self._page > 1)
        self._next_button.setEnabled(self._page < self._total_pages)

    def _go_prev(self) -> None:
        if self._page > 1:
            self.page_changed.emit(self._page - 1)

    def _go_next(self) -> None:
        if self._page < self._total_pages:
            self.page_changed.emit(self._page + 1)
