"""Shared shape for a not-yet-implemented module page: PageHeader +
EmptyStateWidget. Used by the sidebar modules with no backing entity/service
yet (Products, Warehouses, Suppliers, Customers, Reports, Users, Settings)
so each honestly says "not built yet" instead of showing fabricated data.
"""
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget


class PlaceholderPage(QWidget):
    def __init__(self, title: str, subtitle: str, icon: str, message: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader(title, subtitle))
        layout.addWidget(EmptyStateWidget(f"No {title.lower()} yet", icon=icon,
                                          message=message), stretch=1)
