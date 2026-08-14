"""Suppliers page — placeholder. Party records exist (see Purchases'
vendor field) but aren't typed as supplier vs. customer yet, so a list here
would either show everyone or guess wrong — neither is honest, so this
stays an empty state until that distinction exists.
"""
from app.ui.widgets.placeholder_page import PlaceholderPage


class SuppliersPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Suppliers", "Manage vendor relationships.", "🚚",
                         "Dedicated supplier records are coming in a future update.")
