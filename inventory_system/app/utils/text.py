"""Reuses storage.py's spreadsheet formula-injection guard rather than
duplicating it — see storage._safe_text's docstring for what it protects
against. Still relevant to the new layers because the Phase 2/3 Excel
export and PDF paths both round-trip free-text fields (vendor, address).
"""
from storage import _safe_text as safe_text
