"""PySide6 GUI — the whole user interface of the application.

Hard rule for everything under app.ui: widgets receive Services via their
constructor and only ever call app.services / app.schemas. No widget may
import sqlalchemy or a repository directly, and no widget may contain
business logic (totals, validation, stock rules) — that belongs in
app.domain, reached through a Service.
"""
