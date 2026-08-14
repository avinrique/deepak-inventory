"""New layered architecture (GUI -> Services -> Business Logic -> Repositories
-> SQLAlchemy -> PostgreSQL). See docs/architecture.md.

Additive only: inventory_app.py and storage.py do not import anything from
this package yet.
"""
