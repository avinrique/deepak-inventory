"""Application service for generating reports/PDFs. Phase 3 — not wired
up yet; see app.reports.invoice_pdf and docs/architecture.md.
"""
from app.reports.invoice_pdf import render_invoice
from app.schemas.bill import BillOut


class ReportService:
    def invoice_pdf(self, bill: BillOut, out_path: str) -> str:
        return render_invoice(bill, out_path)
