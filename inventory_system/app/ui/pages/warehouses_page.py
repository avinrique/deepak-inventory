"""Warehouses page — placeholder; no Warehouse/location entity exists yet."""
from app.ui.widgets.placeholder_page import PlaceholderPage


class WarehousesPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Warehouses", "Manage storage locations.", "🏬",
                         "Multi-location stock tracking is coming in a future update.")
