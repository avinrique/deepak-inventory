"""Sidebar navigation. Built on QListWidget rather than a column of
QPushButtons specifically for free, correct keyboard navigation — Up/Down
moves the selection and Enter/Space activates it, with no custom event
handling needed.
"""
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from app.ui.theme import SIDEBAR_BG, SIDEBAR_MUTED, scale


@dataclass(frozen=True)
class SidebarModule:
    key: str
    label: str
    icon: str


class Sidebar(QWidget):
    module_selected = Signal(str)

    # Design-pixel widths for the two states; both go through theme.scale().
    EXPANDED_WIDTH = 220
    COLLAPSED_WIDTH = 56

    def __init__(self, modules: list[SidebarModule], default_tax_percent=None):
        super().__init__()
        self.setFixedWidth(scale(self.EXPANDED_WIDTH))
        self.setStyleSheet(f"background: {SIDEBAR_BG};")
        self._keys: list[str] = [m.key for m in modules]
        self._modules = list(modules)
        self._collapsed = False
        self._default_tax_percent = default_tax_percent

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

        self._brand = brand
        self._brand_sub = sub

        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for module in modules:
            item = QListWidgetItem(f"  {module.icon}   {module.label}")
            item.setData(Qt.ItemDataRole.UserRole, module.key)
            # Always set: in the collapsed state the label is only available
            # as a tooltip, and it costs nothing to have one when expanded.
            item.setToolTip(module.label)
            self._list.addItem(item)
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list, stretch=1)

        self._footer = QLabel("  ")
        self._footer.setStyleSheet(
            f"color: {SIDEBAR_MUTED}; font-size: 10px; padding: 16px 22px;")
        layout.addWidget(self._footer)
        self.set_default_tax_percent(default_tax_percent)

        self._list.setCurrentRow(0)

    def set_collapsed(self, collapsed: bool) -> None:
        """Icon-only mode for narrow windows.

        Nothing is removed -- every entry stays in the list, keyboard
        navigation is unchanged and the labels become tooltips -- so this is
        purely a width trade, not a reduction in what the user can reach.
        """
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(scale(self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH))
        self._brand.setVisible(not collapsed)
        self._brand_sub.setVisible(not collapsed)
        self._footer.setVisible(not collapsed)
        for row, module in enumerate(self._modules):
            item = self._list.item(row)
            item.setText(f"  {module.icon}" if collapsed
                         else f"  {module.icon}   {module.label}")

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_default_tax_percent(self, percent) -> None:
        """Fillable after construction: MainWindow loads this on a worker so
        the window can paint before the round trip finishes.

        Formatting is guarded because this is a worker callback — an
        unexpected value would otherwise raise inside a Qt slot, where there
        is no caller to catch it, over a decorative footer label.
        """
        self._default_tax_percent = percent
        try:
            text = f"  VAT {percent:g}% default" if percent is not None else "  "
        except (TypeError, ValueError):
            text = "  "
        self._footer.setText(text)

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        self.module_selected.emit(self._keys[row])

    def select(self, key: str) -> None:
        if key in self._keys:
            self._list.setCurrentRow(self._keys.index(key))
