"""Customers page — placeholder; see suppliers_page.py's docstring for why
(party records aren't typed supplier vs. customer yet).
"""
from app.ui.widgets.placeholder_page import PlaceholderPage


class CustomersPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Customers", "Manage customer relationships.", "👥",
                         "Dedicated customer records are coming in a future update.")
