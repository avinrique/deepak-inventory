"""Reports page — placeholder. app/reports/invoice_pdf.py exists as a
fixed-signature stub (Phase 3, no layout implemented) — nothing to show yet.
"""
from app.ui.widgets.placeholder_page import PlaceholderPage


class ReportsPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Reports", "Sales, purchase, and stock reports.", "📈",
                         "Report generation is coming in a future update.")
