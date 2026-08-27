# Architecture

Status: **shipping.** The migration this document describes is finished and
the application is packaged as a Windows installer — see
[`deployment.md`](deployment.md) for how a build is produced and deployed.

The architecture is:

```
PySide6 GUI  →  Application Services  →  Business Logic  →  Repositories  →  SQLAlchemy  →  PostgreSQL
```

It replaced:

```
Tkinter GUI  →  storage.py  →  Excel
```

The legacy Tkinter application still exists at the repository root
(`../inventory_app.py`, `../storage.py`) and still runs, but **nothing in
this project imports it**. The `excel/` repositories and the
`INVENTORY_BACKEND` flag that chose between them and PostgreSQL have been
deleted: the Excel path reached outside this directory through a `sys.path`
shim, which cannot survive being packaged, and the only service it still fed
had already been superseded by `InventoryService`'s warehouse ledger.

## Layers

| Layer | Package | Responsibility | Depends on |
|---|---|---|---|
| UI | `app/ui` (PySide6) | Widgets, layout, user input/output only — no DB queries, no business logic | Services |
| Application Services | `app/services` | One class per use case (create a sale, list stock...); orchestrates domain + repositories; the only thing UI code calls | Business Logic, Repositories |
| Business Logic | `app/domain` | Pure calculation/validation rules (bill totals, VAT/ECS math, stock-sufficiency). No I/O, no framework, `Decimal` throughout | nothing |
| Repositories | `app/repositories` | One `Protocol` interface per aggregate in `interfaces.py`, with a SQLAlchemy implementation in `sql/` | Models |
| Models | `app/models` | SQLAlchemy 2.x declarative ORM classes | Database |
| Schemas | `app/schemas` | Pydantic DTOs — the *only* objects that cross the UI ⇄ Service boundary | nothing |
| Database | `app/database` | Engine/session plumbing, read from `app/config` | Models |
| Config | `app/config` | Settings, layered environment > per-user config file > dev `.env` > defaults; the config file's database password is encrypted with Windows DPAPI | Core (paths) |
| Security | `app/security` | `CurrentUser` context; single fixed local user today, real auth can be swapped in later without changing Service signatures | nothing |
| Reports | `app/reports` | ReportLab PDF generation | Schemas |
| Workers | `app/workers` | `QRunnable` background jobs so Excel/DB I/O never blocks the Qt UI thread | Services |
| Core | `app/core` | Path resolution, DI composition root, app-wide exceptions, logging, crash handler, version | everything (wires it together) |
| Utilities | `app/utils` | `Decimal` parsing, shared text helpers | nothing |

Rules enforced by this structure (see `app/ui/__init__.py`'s docstring):
**no SQL access in widgets, no business logic in widgets.** A widget's
constructor receives its Service(s); it never imports `sqlalchemy` or a
repository directly.

## Folder structure

```
deepak-inventory/                    (repo root)
├── inventory_app.py                  # legacy Tkinter GUI — retired, still runnable
├── storage.py                        # legacy Excel I/O — retired, no longer imported
├── .github/workflows/
│   └── windows-build.yml              # builds, self-tests and packages the app
│
└── inventory_system/                  # the application
    ├── README.md
    ├── pyproject.toml                  # pytest config, black/ruff settings
    ├── requirements.txt                 # runtime only — everything here ships
    ├── requirements-dev.txt              # tests, PyInstaller, linting
    ├── .env.example                       # development configuration only
    ├── conftest.py                         # rootdir marker + headless Qt platform
    ├── alembic.ini
    ├── migrations/versions/                 # 19 revisions, shipped with the app
    ├── packaging/
    │   ├── InventoryManagementSystem.spec    # PyInstaller (onedir)
    │   ├── installer.iss                      # Inno Setup
    │   ├── app.ico / make_icon.py              # icon, generated from code
    │   ├── make_version_info.py                 # Windows VERSIONINFO resource
    │   ├── fetch_pgtools.py                      # stages pg_dump/pg_restore
    │   └── build_windows.ps1                      # local build
    ├── scripts/
    │   ├── run_app.py                              # convenience launcher
    │   └── init_db.py                               # migrate, seed, create first owner
    ├── docs/
    │   ├── architecture.md                           # this file
    │   └── deployment.md                              # build/install/operate
    ├── tests/
    └── app/
        ├── main.py                                     # ordered startup sequence
        ├── selftest.py                                  # verifies a packaged build
        ├── __version__.py                                # the one place VERSION lives
        ├── core/                                          # paths, container, exceptions,
        │                                                   # logging, crash handler
        ├── config/                                         # settings.py, store.py (DPAPI)
        ├── ui/                                              # main_window, login, setup
        │                                                     # wizard, pages/, widgets/
        ├── domain/                                           # Decimal math and validation
        ├── schemas/                                           # Pydantic DTOs
        ├── repositories/{interfaces.py,sql/}
        ├── services/
        ├── models/
        ├── database/                                           # session, schema_check,
        │                                                        # errors, bootstrap
        ├── security/
        ├── reporting/ reports/ backup/
        ├── workers/
        └── utils/
```

## How the application starts

The sequence in `app/main.py` is ordered deliberately — each step can fail,
and every failure has to reach the user as something they can act on rather
than as a window that never appears:

1. create the per-user data directories (never inside the install directory);
2. configure logging, so every later failure is recorded;
3. install the crash handler, so an unhandled error is never silent;
4. set the High-DPI rounding policy — must precede `QApplication`;
5. create `QApplication`, set its identity and icon;
6. load the theme, a bundled resource that a broken package can lack;
7. ensure a database is configured, or run the setup wizard;
8. connect and check the schema, distinguishing *unreachable* from
   *wrong credentials* from *out of date*;
9. build the container and show the login window.

Steps 6-8 are the ones that only exist because the application is packaged.
Running from a source checkout, the stylesheet is always present, the `.env`
is always found, and the developer knows what a stack trace means.

## Why PySide6

PySide6 (Qt) was chosen for the production rewrite — richer table/tree
widgets, proper threading (`QRunnable`/signals) so I/O never blocks the UI,
native look across platforms. Rather than retrofit Tkinter widgets in
`inventory_app.py`, the new UI is built fresh under `inventory_system/app/ui`
against the same Service layer, so the legacy app keeps working unmodified
for current users while the rewrite catches up feature-by-feature.

## Why `Decimal`, and why it matters here

The legacy `calculate_total()` in `inventory_app.py` uses Python `float` +
`round()`, which is not exact for money (`round(2.675, 2) == 2.67` in binary
float, not `2.68`). `app/domain/billing.py` uses `decimal.Decimal` with
explicit `ROUND_HALF_UP` quantization to two places instead. The legacy
`storage.num()` is intentionally left as-is (it's part of the untouched
Excel path); `app/utils/money.to_decimal()` is its strict counterpart, used
by everything under `app/` — it raises on unparseable input instead of
silently returning `0`.

## Identity/access-control schema

| Entity | Key design choices |
|---|---|
| `User` | Email uniqueness is a functional unique index (`ix_users_email`) plus a `CHECK (email = lower(email))` — the index alone wouldn't catch `A@b.com` vs `a@b.com`. `is_superuser` is a deliberate platform-admin escape hatch; a user's *ordinary* role is per-organization (below), not global. |
| `Role` / `Permission` | Global catalogs, not duplicated per organization — "Owner" means the same permission set everywhere. `RolePermission` is a pure join with a **composite PK** (`role_id`, `permission_id`), not a surrogate UUID: the pair already is the identity, so a separate id would be pure denormalization. |
| `Organization` | The tenant. Inventory entities (added later) each carry `organization_id`. |
| `UserOrganization` | Membership + role-within-that-org. Composite PK (`user_id`, `organization_id`): a user belongs to a given org at most once. `role_id` is `ON DELETE RESTRICT` — a role in active use can't be deleted out from under a member; the caller must reassign first. |
| `AuditLog` | Append-only (no `updated_at`, no soft-delete — only `INSERT` is legitimate). `organization_id`/`user_id` are `ON DELETE SET NULL` so the trail outlives the org/user; `actor_email`/`organization_name` are captured as a point-in-time snapshot specifically *because* the FK is allowed to go null later — the one deliberate exception to "avoid unnecessary denormalization" in this schema, and it's called out in the model's docstring. |

All UUID PKs are generated **client-side** (`default=uuid.uuid4`, not a
server default) — an object's `id` is available immediately after
construction, before flush/commit, and there's no PostgreSQL-version
dependency for id generation.

Money fields use `Numeric`/`Decimal` end-to-end (see the `Decimal` section
above) — no inventory entities exist yet in this phase, but the same rule
applies the moment `Bill`/`BillLine` land.

The initial migration was generated with `alembic revision --autogenerate`
and applied against a real local PostgreSQL 16 instance to confirm it
actually runs (not hand-written and assumed correct) — see
`tests/models/test_database_integration.py` for the checks that were run:
transactional commit, relationship loading, unique/check-constraint
rejection, `ON DELETE RESTRICT` enforcement, and rollback-on-exception. The
follow-up migration adding `users.must_change_password`
(`migrations/versions/205f1c3347a5_...py`) needed a hand fix autogenerate
couldn't produce: it added the column `NOT NULL` with no default, which
fails against a `users` table that already has rows — verified by inserting
a row via `psql` first, then confirming the corrected migration (adds with
a `server_default`, then drops it, matching the model's client-side-only
default) backfills it to `false` and applies cleanly.

## Authentication/authorization design

This is a desktop app, so "session" means **one user logged into the
running GUI process at a time** — e.g. a shared terminal at a shop counter
where staff log in/out over a shift — not a web session with cookies or
concurrent multi-device logins. `SessionManager`
(`app/security/session.py`) holds that single session in memory, tracked by
**idle time** (`session_idle_timeout_minutes` in `.env`, default 30): every
permission check *is* activity and extends it, so continuous use never
times out but walking away eventually does. `SessionManager` never calls
`datetime.now()` itself — callers pass `now` explicitly — which is what
makes 12 of the session/authorization tests fully deterministic without
sleeping or monkeypatching the clock.

**The enforcement boundary is `app.security.authorization`, not the UI.**
`@require_permission("sales.create")` decorates a Service method, looks up
the current session via `self._sessions` (every protected Service takes a
`SessionManager` in its constructor, the same convention as any other
injected dependency), and raises before the method body runs at all if:
the session doesn't exist or has timed out (`NotAuthenticatedError` /
`SessionExpiredError`), the session's user must change their password first
(`PasswordChangeRequiredError` — set after an admin-initiated reset, cleared
by a successful `AuthService.change_password`, checked *before* the
permission itself so a forced-reset user can't do anything else until they
comply), or the permission code isn't in the session's set
(`PermissionDeniedError`) — unless `is_superuser`, which bypasses the
permission (not the password-change) check.
`tests/services/test_user_service.py` proves this the direct way: it calls
`UserService.deactivate_user()` with an unauthorized session and asserts
the fake repository was never touched — the same call a compromised or
buggy UI would make, rejected at the only layer that matters.

**Passwords:** Argon2id via `argon2-cffi` (`app/security/passwords.py`),
self-describing hashes so `verify_password` needs no stored parameters, and
`needs_rehash`/`AuthService.login`'s rehash-on-success path so a future
cost-parameter change migrates users transparently instead of forcing a
mass reset. `User.hashed_password` never holds anything else — verified by
`test_authorized_create_user_hashes_the_password_not_plaintext`.

**Password reset:** there's no email/SMS infrastructure in a desktop app,
so `UserService.reset_password` (itself `users.reset_password`-gated) is the
realistic mechanism — generates a random temporary password, returns it
*once* for the admin to relay out-of-band, and sets
`must_change_password=True` so the temporary password only ever unlocks
`AuthService.change_password`, nothing else, until it's replaced.

**Roles/permissions:** `app/security/permissions.py` is the single source
of truth for the 8 roles and 18 permission codes — see the module
docstring for the separation-of-duties reasoning (e.g. why `SALES_STAFF`
can create/read sales but not cancel or refund them). `scripts/init_db.py`
seeds it; changing the dict after go-live affects only newly-seeded
databases, not existing grants.

## Testing strategy

- `tests/domain` — pure `Decimal` math and validation rules, no I/O. Fastest,
  run on every change.
- `tests/security` — password hashing (Argon2id round-trip, `needs_rehash`),
  `SessionManager` (idle timeout, deterministic via injected `now`), and
  `@require_permission`/`check_permission` — no I/O.
- `tests/services` — Services tested against hand-written fake repositories
  implementing the `Protocol`s — no database. This is where most
  authorization tests live: every `UserService` method is exercised both as
  an authorized call (does the right thing) and an unauthorized one
  (repository never touched).
- `tests/repositories`, `tests/models` — integration tests against a **real**
  PostgreSQL database. They create and then drop every table, so they are
  gated on `INVENTORY_TEST_DATABASE_URL` — deliberately a *different* setting
  from the application's own `INVENTORY_DATABASE_URL`, so the suite cannot do
  that to a real database. Unset (the default) skips them. They do not run
  against SQLite: UUID/JSONB/INET are PostgreSQL-specific.
- `tests/ui` — widget construction, layout and permission-driven visibility,
  against `MagicMock` services. Includes `test_responsive_layout.py`, which
  asserts that no window or dialog demands more space than a 1366x768 screen
  offers at each Windows scaling factor, and `test_build_page_smoke.py`,
  which constructs every page.
- `tests/config`, `tests/core` — the packaging-critical behaviour: settings
  precedence, the config file's password handling, path resolution, log
  rotation and the crash handler.

`conftest.py` selects Qt's offscreen platform when there is no display, so
the UI tests genuinely run in CI instead of skipping themselves and leaving
the build green.

```bash
cd inventory_system
pip install -r requirements-dev.txt
pytest                                     # DB tests skip without a test database

# to also exercise the database layer, against a scratch database only:
export INVENTORY_TEST_DATABASE_URL=postgresql+psycopg://localhost:5432/inventory_test
pytest
```

### Verifying a packaged build

The tests above run against the source tree, where the failures that matter
most for a packaged application cannot happen: the stylesheet is always
present, `openpyxl` is always importable, Alembic can always find its
migrations. `app/selftest.py` covers that gap — `--self-test` loads every
bundled resource, imports the drivers that are only ever named inside
strings, renders a PDF and a spreadsheet, and constructs every page and
window, then exits. CI runs it **against the built executable**, which is
the only way those defects surface before a user finds them.

## Deliberate limitations

- **No auto-updater.** A fixed installer `AppId` means a newer installer
  upgrades in place cleanly, which is enough for a handful of shop machines;
  a background updater would need signing infrastructure and a release
  channel that do not exist yet.
- **The application is a database client.** It does not embed a database, so
  an installation needs a PostgreSQL server reachable over the network —
  cloud or LAN. There is no offline mode and no local cache.
- **Connection settings roam, backups do not.** `config.json` is in roaming
  AppData so a domain user keeps it between machines; logs and backups are
  machine-local by design.
- **No row-level security.** Multi-tenancy is enforced by `organization_id`
  scoping in the repository layer, not by the database.
- **Default connection pool.** `app/database/session.py` sets
  `pool_pre_ping` and `pool_recycle` — which a long-idle desktop client
  genuinely needs — but otherwise leaves SQLAlchemy's defaults, which are
  sized for one user per process rather than tuned for load.
