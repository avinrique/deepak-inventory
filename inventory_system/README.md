# Inventory Management System

A PySide6 + SQLAlchemy + PostgreSQL desktop application for inventory,
purchasing, sales and invoicing, packaged as a Windows installer that a
business user can run without Python or any development tooling.

- Building and releasing, installing, configuring a deployment, and
  troubleshooting: [`docs/deployment.md`](docs/deployment.md)
- Internal design and layering rules: [`docs/architecture.md`](docs/architecture.md)

The legacy Tkinter/Excel application it replaces still sits at the repository
root (`../inventory_app.py`, `../storage.py`). Nothing in this project
imports it any more.

## Layout

| Path | Layer |
|---|---|
| `app/main.py` | Entry point — the ordered startup sequence |
| `app/selftest.py` | `--self-test`: verifies a *packaged* build without a database |
| `app/ui/` | PySide6 widgets — no DB queries, no business logic |
| `app/services/` | Application services — the only thing UI code calls |
| `app/domain/` | Pure business logic (Decimal-based), no I/O |
| `app/repositories/` | `interfaces.py` Protocols and their `sql/` implementations |
| `app/models/` | SQLAlchemy ORM models |
| `app/schemas/` | Pydantic DTOs crossing the UI ⇄ Service boundary |
| `app/database/` | Engine/session, startup schema check, driver-error translation, first-run bootstrap |
| `app/config/` | Settings and the per-user config file (DPAPI-encrypted password) |
| `app/security/` | Argon2id passwords, the 8-role permission catalog, idle-timeout `SessionManager`, and `@require_permission` — the actual authorization boundary, not the UI |
| `app/reporting/`, `app/reports/` | CSV/Excel/PDF export and printing; ReportLab invoices |
| `app/backup/` | `pg_dump`/`pg_restore` wrapper |
| `app/workers/` | Background `QRunnable` workers, keeps I/O off the UI thread |
| `app/core/` | Paths, DI container, exceptions, logging, crash handler, version |
| `migrations/` | Alembic — each migration generated and verified against a real PostgreSQL |
| `packaging/` | PyInstaller spec, Inno Setup script, icon and build scripts |
| `scripts/` | `run_app.py` (launcher), `init_db.py` (migrate + seed + create first owner) |

## Development setup

```bash
cd inventory_system
python -m venv .venv && source .venv/bin/activate    # Python 3.12
pip install -r requirements-dev.txt

cp .env.example .env      # then set INVENTORY_DATABASE_URL
python scripts/init_db.py --create-owner   # only for a brand-new database

pytest
python -m app.main
```

`.env` is development-only and git-ignored. A packaged build ignores it
entirely and reads its settings from the per-user config file the first-run
wizard writes — see [`docs/deployment.md`](docs/deployment.md).

### Useful commands

```bash
pytest                                  # full suite; DB tests skip without a test database
python -m app.main --self-test          # build every screen and export, then exit
python -m app.main --version
QT_SCALE_FACTOR=1.5 python -m app.main  # check a Windows scaling factor

# Screenshot every screen at a given scale — what CI uploads for DPI review.
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 \
  python -m app.main --self-test --screenshot-dir /tmp/shots
```

Database integration tests (`tests/models`, `tests/repositories/test_sql_*`)
run only when `INVENTORY_TEST_DATABASE_URL` points at a **scratch** database
— they create and drop every table. It is deliberately a separate setting
from `INVENTORY_DATABASE_URL` so the suite can never do that to a real one.

### Building the Windows application

CI is the reference build; `packaging/build_windows.ps1` does the same
locally on Windows. See [`docs/deployment.md`](docs/deployment.md).

## Rules enforced by this structure

- UI widgets never run a database query directly — they call an
  `app.services` method, injected via the constructor.
- Widgets never contain business logic (totals, VAT, stock rules) — that is
  `app.domain`, reached through a Service.
- `app.repositories` is the only place persistence details exist; Services
  depend on `app.repositories.interfaces` Protocols, never a concrete class.
- Permission checks happen in the Service layer (`@require_permission`),
  never only in the UI — a hidden button is a convenience, not the boundary.
  Every protected `UserService` method is tested by calling it directly with
  an unauthorized session and asserting the repository was never touched.
- Nothing resolves a path against the current working directory. An
  installed application is launched from a shortcut with an arbitrary
  working directory, so every path goes through `app.core.paths`.
- No credential is ever built into the binary, printed, or logged.
