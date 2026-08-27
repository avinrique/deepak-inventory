"""Category/Brand/Unit management — three CrudListPanel instances in tabs.
Opened from the Products page toolbar ("Manage Catalog").
"""
from PySide6.QtWidgets import QDialog, QTabWidget, QVBoxLayout

from app.schemas.product import BrandCreate, CategoryCreate, UnitCreate
from app.services.catalog_service import CatalogService
from app.ui.theme import STYLESHEET
from app.ui.widgets.crud_list_panel import CrudListPanel
from app.ui.widgets.responsive import fit_to_screen


class CatalogManagerDialog(QDialog):
    def __init__(self, catalog_service: CatalogService, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Catalog")
        fit_to_screen(self, 560, 420, minimum_width=420, minimum_height=300)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        tabs.addTab(CrudListPanel(
            fields=[("Name", "name"), ("Description", "description")],
            optional_fields={"description"}, display=lambda c: c.name,
            schema_cls=CategoryCreate, list_fn=catalog_service.list_categories,
            create_fn=catalog_service.create_category,
            update_fn=catalog_service.update_category,
            delete_fn=catalog_service.delete_category), "Categories")

        tabs.addTab(CrudListPanel(
            fields=[("Name", "name"), ("Description", "description")],
            optional_fields={"description"}, display=lambda b: b.name,
            schema_cls=BrandCreate, list_fn=catalog_service.list_brands,
            create_fn=catalog_service.create_brand, update_fn=catalog_service.update_brand,
            delete_fn=catalog_service.delete_brand), "Brands")

        tabs.addTab(CrudListPanel(
            fields=[("Name", "name"), ("Abbreviation", "abbreviation")],
            optional_fields=set(), display=lambda u: f"{u.name} ({u.abbreviation})",
            schema_cls=UnitCreate, list_fn=catalog_service.list_units,
            create_fn=catalog_service.create_unit, update_fn=catalog_service.update_unit,
            delete_fn=catalog_service.delete_unit), "Units")
