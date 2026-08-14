# Inventory System (in progress)

Production-grade rewrite of the Inventory Management app — PySide6 +
SQLAlchemy + PostgreSQL — replacing the legacy Tkinter/Excel app one slice
at a time. **The legacy app, one directory up (`../inventory_app.py`), is
unaffected and is still what real users run today.** Nothing here is
feature-complete yet. See [`docs/architecture.md`](docs/architecture.md)
for the full design and migration plan.

## Layout

| Path | Layer |
|---|---|
| `app/main.py` | Entrypoint |
| `app/ui/` | PySide6 widgets — no DB queries, no business logic |
| `app/services/` | Application services — the only thing UI code calls |
| `app/domain/` | Pure business logic (Decimal-based), no I/O |
| `app/repositories/` | `excel/` (real, wraps the legacy app) and `sql/` — `user_repository.py` is real, `bill/stock/party` are Phase 2 stubs |
| `app/models/` | SQLAlchemy ORM models — `User`, `Role`, `Permission`, `RolePermission`, `Organization`, `UserOrganization`, `AuditLog` (inventory entities not yet added) |
| `app/schemas/` | Pydantic DTOs crossing the UI ⇄ Service boundary |
| `app/database/` | SQLAlchemy engine/session |
| `app/config/` | Settings, loaded from `.env` — never hardcoded |
| `app/security/` | Argon2id passwords, the 8-role permission catalog, idle-timeout `SessionManager`, and `@require_permission` — the actual authorization enforcement boundary, not the UI |
| `app/reports/` | ReportLab PDF generation (Phase 3, stub) |
| `app/workers/` | Background `QRunnable` workers, keeps I/O off the UI thread |
| `app/core/` | DI container, app-wide exceptions, logging setup |
| `migrations/` | Alembic — schema migrations, each generated and verified against a real local PostgreSQL |
| `scripts/` | `run_app.py` (launcher), `init_db.py` (runs migrations + seeds the 8-role/permission catalog) |
| `resources/` | Icons/stylesheets (currently empty) |

## Setup

```bash
cd inventory_system
pip install -r requirements.txt
cp .env.example .env   # defaults (backend=excel) work with no edits

pytest                  # runs everything except tests/models (needs Postgres, see below)
python -m app.main
```

### Database (optional — only if you have PostgreSQL)

```bash
# in .env: INVENTORY_DATABASE_URL=postgresql+psycopg://...  (always used for
# auth, regardless of INVENTORY_BACKEND — see app/core/container.py)
python scripts/init_db.py   # runs Alembic migrations, seeds the 8-role/permission catalog — idempotent
pytest                       # now also runs tests/models + tests/repositories/test_sql_user_repository.py
```

`AuthService`/`UserService` (login, logout, password change/reset,
activation) are real and fully tested against this — see
[`docs/architecture.md`](docs/architecture.md)'s "Authentication/
authorization design" section. Nothing under `app/ui` calls them yet
(no login screen), and `app/repositories/sql/{bill,stock,party}.py` are
still stubs — this doesn't wire the inventory side of the app to the
database, only auth.

## Rules enforced by this structure

- UI widgets never run a database/Excel query directly — they call an
  `app.services` method, injected via the constructor.
- Widgets never contain business logic (totals, VAT, stock rules) — that's
  `app.domain`, reached through a Service.
- `app.repositories` is the only place persistence details (Excel today,
  SQL later) exist; Services depend on `app.repositories.interfaces`
  Protocols, never a concrete implementation.
- Configuration comes from `.env` / environment variables
  (`app/config/settings.py`) — nothing is hardcoded, `.env` is git-ignored.
- Permission checks happen in the Service layer (`@require_permission`),
  never only in the UI — a UI hiding a button is a convenience, not the
  boundary. Every protected `UserService` method is tested by calling it
  directly with an unauthorized session and asserting the repository was
  never touched.
