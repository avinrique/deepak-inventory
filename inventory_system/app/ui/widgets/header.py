"""Top header: breadcrumb on the left, notification bell + user menu on the
right. Purely layout — composes BreadcrumbBar/NotificationBell/UserMenu,
owns none of their behavior.
"""
from PySide6.QtWidgets import QFrame, QHBoxLayout

from app.ui.widgets.breadcrumb import BreadcrumbBar
from app.ui.widgets.notification_bell import NotificationBell
from app.ui.widgets.user_menu import UserMenu
from app.ui.theme import scale


class Header(QFrame):
    def __init__(self, full_name: str, role_label: str):
        super().__init__()
        self.setObjectName("header")
        self.setFixedHeight(scale(64))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 0, 24, 0)
        layout.setSpacing(16)

        self.breadcrumb = BreadcrumbBar()
        layout.addWidget(self.breadcrumb)
        layout.addStretch()

        self.notification_bell = NotificationBell()
        layout.addWidget(self.notification_bell)

        self.user_menu = UserMenu(full_name, role_label)
        layout.addWidget(self.user_menu)
