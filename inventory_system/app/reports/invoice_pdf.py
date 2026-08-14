"""PDF invoice generation via ReportLab. Phase 3 — not implemented yet.

Signature is fixed now so ReportService/tests can be written against it
before the actual PDF layout is designed.
"""
from app.schemas.bill import BillOut


def render_invoice(bill: BillOut, out_path: str) -> str:
    raise NotImplementedError("Phase 3 — see docs/architecture.md")
