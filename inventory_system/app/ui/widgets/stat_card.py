"""Dashboard stat tile: icon + label + big value, on the shared card style."""
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.theme import MUTED, TEXT


class StatCard(QWidget):
    def __init__(self, label: str, value: str, icon: str, accent: str):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(6)

        icon_label = QLabel(icon)
        icon_label.setStyleSheet(f"font-size: 20px;")
        layout.addWidget(icon_label)

        self._value_label = QLabel(value)
        self._value_label.setStyleSheet(
            f"font-size: 26px; font-weight: 700; color: {accent};")
        layout.addWidget(self._value_label)

        label_widget = QLabel(label)
        label_widget.setStyleSheet(f"font-size: 12px; color: {MUTED}; font-weight: 600;")
        layout.addWidget(label_widget)

    def set_value(self, value: str) -> None:
        self._value_label.setText(value)
