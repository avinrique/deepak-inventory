"""PySide6 GUI — the in-progress replacement for the legacy Tkinter
inventory_app.py (which stays at the repo root, unchanged, and is what real
users run until this is feature-complete).

Hard rule for everything under app.ui: widgets receive Services via their
constructor and only ever call app.services / app.schemas. No widget may
import storage, sqlalchemy, or a repository directly, and no widget may
contain business logic (totals, validation, stock rules) — that belongs in
app.domain, reached through a Service.
"""
