"""Sidebar navigation. Built on QListWidget rather than a column of
QPushButtons specifically for free, correct keyboard navigation — Up/Down
moves the selection and Enter/Space activates it, with no custom event
handling needed.
"""
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from app.ui.theme import SIDEBAR_BG, SIDEBAR_MUTED


@dataclass(frozen=True)
class SidebarModule:
    key: str
    label: str
    icon: str


class Sidebar(QWidget):
    module_selected = Signal(str)

    def __init__(self, modules: list[SidebarModule]):
        super().__init__()
        self.setFixedWidth(220)
        self.setStyleSheet(f"background: {SIDEBAR_BG};")
        self._keys: list[str] = [m.key for m in modules]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        brand = QLabel("  Inventory")
        brand.setStyleSheet("color: white; font-size: 17px; font-weight: 700;"
                            "padding: 26px 22px 2px 22px;")
        layout.addWidget(brand)
        sub = QLabel("  MANAGEMENT")
        sub.setStyleSheet(f"color: {SIDEBAR_MUTED}; font-size: 10px; font-weight: 700;"
                          "letter-spacing: 1px; padding: 0 22px 22px 22px;")
        layout.addWidget(sub)

        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for module in modules:
            item = QListWidgetItem(f"  {module.icon}   {module.label}")
            item.setData(Qt.ItemDataRole.UserRole, module.key)
            self._list.addItem(item)
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, stretch=1)

        footer = QLabel("  VAT 13% default")
        footer.setStyleSheet(f"color: #475569; font-size: 10px; padding: 16px 22px;")
        layout.addWidget(footer)

        self._list.setCurrentRow(0)

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        self.module_selected.emit(self._keys[row])

    def select(self, key: str) -> None:
        if key in self._keys:
            self._list.setCurrentRow(self._keys.index(key))
