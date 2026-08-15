"""User profile button + dropdown menu in the header: shows who's logged in
and their role, with My Profile / Change Password / Log Out actions. The
menu itself is a QMenu, so it's already keyboard-navigable (arrows, Enter,
Escape) with no extra work.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QVBoxLayout, QWidget

from app.ui.theme import MUTED, TEXT
from app.ui.widgets.avatar import AvatarLabel


class UserMenu(QWidget):
    change_password_requested = Signal()
    profile_requested = Signal()
    logout_requested = Signal()

    def __init__(self, full_name: str, role_label: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(AvatarLabel(full_name, size=34, font_size=12))

        text_box = QVBoxLayout()
        text_box.setSpacing(0)
        name_label = QLabel(full_name)
        name_label.setStyleSheet(f"font-size: 12px; font-weight: 700; color: {TEXT};")
        role_display = QLabel(role_label)
        role_display.setStyleSheet(f"font-size: 10px; color: {MUTED};")
        text_box.addWidget(name_label)
        text_box.addWidget(role_display)
        layout.addLayout(text_box)

        self._button = QToolButton()
        self._button.setText("▾")
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.setStyleSheet("border: none; font-size: 11px; color: " + MUTED + ";")
        self._button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        menu = QMenu(self._button)
        profile_action = menu.addAction("My Profile")
        profile_action.triggered.connect(self.profile_requested.emit)
        change_password_action = menu.addAction("Change Password")
        change_password_action.triggered.connect(self.change_password_requested.emit)
        menu.addSeparator()
        logout_action = menu.addAction("Log Out")
        logout_action.triggered.connect(self.logout_requested.emit)
        self._button.setMenu(menu)

        layout.addWidget(self._button)
