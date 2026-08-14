# Upgrade Architecture

Status: **Phase 0 — scaffold only.** `../inventory_app.py` and
`../storage.py` (the legacy app, one directory up) are untouched and run
exactly as before. Nothing in `inventory_system/` is wired into the legacy
GUI, and the PySide6 GUI here is a placeholder shell — no page does real
work yet.

## Goal

Move gradually from:

```
Tkinter GUI  →  storage.py  →  Excel
```

toward:

```
PySide6 GUI  →  Application Services  →  Business Logic  →  Repositories  →  SQLAlchemy  →  PostgreSQL
```

without a rewrite: the legacy Tkinter app, `storage.py`, and the Excel
files stay authoritative and working for real users through the entire
migration, and are only retired once `inventory_system/` has been proven
equivalent and feature-complete.

## Layers

| Layer | Package | Responsibility | Depends on |
|---|---|---|---|
| UI | `app/ui` (PySide6) | Widgets, layout, user input/output only — no DB queries, no business logic | Services |
| Application Services | `app/services` | One class per use case (create a sale, list stock...); orchestrates domain + repositories; the only thing UI code calls | Business Logic, Repositories |
| Business Logic | `app/domain` | Pure calculation/validation rules (bill totals, VAT/ECS math, stock-sufficiency). No I/O, no framework, `Decimal` throughout | nothing |
| Repositories | `app/repositories` | One `Protocol` interface per aggregate (`BillRepository`, `StockRepository`, `PartyRepository`), each with an `excel/` and a `sql/` implementation | Models (sql impl) / legacy `storage.py` (excel impl) |
| Models | `app/models` | SQLAlchemy 2.x declarative ORM classes | Database |
| Schemas | `app/schemas` | Pydantic DTOs — the *only* objects that cross the UI ⇄ Service boundary | nothing |
| Database | `app/database` | Engine/session plumbing, read from `app/config` | Models |
| Config | `app/config` | Settings loaded from environment/`.env` — never hardcoded | nothing |
| Security | `app/security` | `CurrentUser` context; single fixed local user today, real auth can be swapped in later without changing Service signatures | nothing |
| Reports | `app/reports` | ReportLab PDF generation | Schemas |
| Workers | `app/workers` | `QRunnable` background jobs so Excel/DB I/O never blocks the Qt UI thread | Services |
| Core | `app/core` | DI composition root, app-wide exceptions, logging setup | everything (wires it together) |
| Utilities | `app/utils` | `Decimal` parsing, shared text helpers | nothing |

Rules enforced by this structure (see `app/ui/__init__.py`'s docstring):
**no SQL/Excel access in widgets, no business logic in widgets.** A widget's
constructor receives its Service(s); it never imports `storage`,
`sqlalchemy`, or a repository directly.

## Folder structure

```
deepak-inventory/                    (repo root)
├── inventory_app.py                  # legacy Tkinter GUI — unchanged
├── storage.py                        # legacy Excel I/O — unchanged
├── requirements.txt                   # unchanged — still all the legacy app needs
│
└── inventory_system/                  # THIS project — production-grade rewrite
    ├── README.md
    ├── pyproject.toml                  # pytest config, black/ruff settings
    ├── requirements.txt                 # PySide6, sqlalchemy, alembic, pydantic, reportlab...
    ├── .env.example                      # every setting app/config/settings.py reads
    ├── .gitignore                         # .env, __pycache__, logs/, ...
    ├── conftest.py
    ├── alembic.ini                         # Phase 2 — not yet run against a real DB
    ├── migrations/
    │   ├── env.py
    │   └── versions/                        # empty until Phase 2
    ├── scripts/
    │   └── run_app.py                        # convenience launcher
    ├── resources/                              # icons/stylesheets (currently empty)
    ├── docs/architecture.md                     # this file
    ├── tests/
    │   ├── domain/                              # pure, no I/O — fastest, run constantly
    │   ├── repositories/                        # excel repo tests against the real legacy storage.py
    │   └── services/                            # fake in-memory repos, no I/O
    └── app/
        ├── main.py                              # QApplication entrypoint
        ├── core/                                 # container.py, exceptions.py, logging_config.py
        ├── config/                               # settings.py (pydantic-settings, reads .env)
        ├── ui/                                    # PySide6: main_window.py, pages/, widgets/
        ├── domain/                                # billing.py (Decimal math/validation)
        ├── schemas/                               # bill.py, stock.py, party.py (Pydantic)
        ├── repositories/
        │   ├── interfaces.py                       # Protocol contracts
        │   ├── excel/                               # thin adapters over ../../storage.py — real, working today
        │   └── sql/                                  # SQLAlchemy-backed — stubs, Phase 2
        ├── services/                                # billing_service.py, stock_service.py, party_service.py, report_service.py
        ├── models/                                   # SQLAlchemy ORM: Bill, BillLine, StockItem, Party — Phase 2
        ├── database/                                  # engine/session — Phase 2
        ├── security/                                   # context.py — CurrentUser stub
        ├── reports/                                     # invoice_pdf.py — Phase 3
        ├── workers/                                      # base_worker.py — QRunnable, not wired up yet
        └── utils/                                         # money.py, text.py
```

### Why `app/repositories/excel` reaches outside this project

`app/repositories/excel/__init__.py` inserts the parent (`deepak-inventory/`)
onto `sys.path` so `import storage` resolves to the legacy app's file. This
is deliberate, not an oversight: `storage.py` is the single source of truth
for the existing `.xlsx` format, and wrapping it avoids a second, divergent
copy of that I/O code. It is the one place `inventory_system/` depends on
something outside its own directory, and it is meant to be deleted (package
and shim both) once the `sql/` backend is the default — see Phase 4 below.

## Migration strategy: strangler fig via a backend flag

`app/repositories/interfaces.py` defines each repository as a `Protocol`.
Services depend only on the Protocol, never on a concrete implementation, so
`app/core/container.py` — the one place that chooses `excel/` vs `sql/` — is
the only code that has to change to swap backends.

1. **Phase 0 (this commit).** Scaffold only. `app/repositories/excel/*` are
   real, thin wrappers around the legacy `storage.py` functions — no
   duplicated logic, no behavior change to the legacy app.
   `app/repositories/sql/*` exist as named stubs (`NotImplementedError`) so
   the second implementation has an obvious home. `app/services` and
   `app/domain` are real and tested. `app/ui` is a real, launchable PySide6
   shell (sidebar + 5 pages) but every page is a placeholder — no page
   reads or writes real data yet.
2. **Phase 1.** Wire each page to its Service: New Bill gets real
   forms/save via `BillingService` (on a `QRunnable` worker, not the UI
   thread), Sales/Purchases/Stock/Parties get real tables via `list_all()`
   (currently `NotImplementedError` in the `excel/` repositories — implement
   when a page actually needs it, rather than speculatively). This phase
   stays on the Excel backend — zero data-migration risk.
3. **Phase 2 (database layer — in progress).** The identity/access-control
   entities (`User`, `Role`, `Permission`, `RolePermission`, `Organization`,
   `UserOrganization`, `AuditLog`) are implemented as real SQLAlchemy 2.0
   models with UUID PKs, timezone-aware timestamps, FKs (with deliberate
   `CASCADE`/`RESTRICT`/`SET NULL` policies per relationship — see each
   model's docstring), unique/check constraints, and indexes — see
   `app/models/`. The initial Alembic migration
   (`migrations/versions/091eeb36e646_initial_schema.py`) was
   autogenerated and applied against a real local PostgreSQL instance to
   confirm it, not hand-written untested; `tests/models/` are real
   integration tests against a live database proving transactions,
   constraint enforcement, and relationship loading (skipped automatically
   when `INVENTORY_BACKEND != "postgres"` or no database is reachable —
   they do not run in this sandbox by default). `scripts/init_db.py` runs
   the migration and idempotently seeds the Role/Permission catalog (see
   Phase 2b below). Inventory-related entities (Bill, StockItem, Party —
   tenant-scoped via `organization_id`, replacing the earlier
   pre-Organization stubs) and `app/repositories/sql/{bill,stock,party}.py`
   are the remaining Phase 2 work. Write **contract tests** once they land:
   the same test bodies run against both `excel/` and `sql/`
   implementations of each repository, proving parity. Add a one-time
   importer (`excel repo.list_all()` → `sql repo.append()` per row) that
   seeds Postgres from a user's existing `.xlsx` history.
4. **Phase 2b (authentication/authorization — done).** `app/security/`:
   Argon2id password hashing, an in-process idle-timeout `SessionManager`,
   and the actual enforcement boundary, `@require_permission`. The 8-role
   permission catalog, `AuthService`, `UserService`, and the first real
   `sql/` repository (`SqlUserRepository` — User has no Excel equivalent to
   fall back on, unlike Bill/Stock/Party) — see "Authentication/
   authorization design" below.
5. **Phase 3.** Implement `app/reports/invoice_pdf.py` (ReportLab) — a new,
   additive capability the legacy app never had.
6. **Phase 4.** Flip `INVENTORY_BACKEND=postgres` by default once parity
   tests are green. Delete `app/repositories/excel/*` and its `sys.path`
   shim; `inventory_system/` no longer depends on anything outside its own
   directory. Retire the legacy Tkinter app once `inventory_system/` covers
   its full feature set.
7. **Phase 5 (optional).** Multi-organization self-service (today, an
   `AuthService.login` caller resolves ambiguous multi-org accounts via an
   explicit `organization_id`, and only `UserService` — already
   `users.manage`-gated — can create users, so there's no self-service
   signup yet).

## Why PySide6, and why not migrate the legacy app in place

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

## Identity/access-control schema (Phase 2)

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

## Authentication/authorization design (Phase 2b)

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
so `UserService.reset_password` (itself `users.manage`-gated) is the
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
- `tests/repositories` — `excel/` repositories tested against the real
  legacy `storage.py` using a temp directory (`INVENTORY_DATA_DIR`
  override). `test_sql_user_repository.py` tests the real `SqlUserRepository`
  (plus `AuthService`/`UserService` wired to it, not fakes) against a live
  database — skips like `tests/models` without one. Once
  `sql/{bill,stock,party}.py` exist, the same `excel/` test bodies run again
  against them (contract tests) to prove parity.
- `tests/services` — `BillingService`/`AuthService`/`UserService` tested
  against hand-written fake repositories implementing the `Protocol`s — no
  Excel, no database. This is where most authorization tests live: every
  `UserService` method is exercised both as an authorized call (does the
  right thing) and an unauthorized one (repository never touched).
- `tests/models` — integration tests against a **real** PostgreSQL database.
  Skipped automatically unless `INVENTORY_BACKEND=postgres` and
  `INVENTORY_DATABASE_URL` points at a reachable database — they do not run
  against SQLite (UUID/JSONB/INET are Postgres-specific types) and do not
  run by default in this sandbox.

Run with:

```bash
cd inventory_system
pip install -r requirements.txt
pytest                                    # tests/models, tests/repositories/test_sql_user_repository.py skip without Postgres

# to also run those and try the database layer for real:
export INVENTORY_BACKEND=postgres
export INVENTORY_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/inventory
python scripts/init_db.py                 # runs migrations + seeds the 8-role catalog
pytest
```

## What's explicitly NOT done in this phase

- No page in `app/ui` reads or writes real data — every page is a
  `QLabel` placeholder, including a login screen: `AuthService`/
  `SessionManager` exist and are fully tested, but nothing in `app/ui`
  calls them yet.
- `app/repositories/sql/{bill,stock,party}.py` still raise
  `NotImplementedError` — `Container` still can't construct a working
  `postgres`-backed `BillingService`. (`SqlUserRepository` is real.)
- Inventory entities (`Bill`, `BillLine`, `StockItem`, `Party`) don't exist
  yet in `app/models` — deliberately out of scope for both the database and
  auth phases, per the requests that started them.
- `app/reports/invoice_pdf.py` has a fixed signature but no PDF layout.
- No account lockout / rate limiting after repeated failed logins — wasn't
  requested and would need a decision on the right policy (lock the
  account? the session? for how long?) rather than a default guess.
- `AuditLog` isn't written to yet by anything in `app/security` or
  `app/services` — the model exists (Phase 2) but login/logout/password
  events aren't recorded there. A reasonable next step, not done here
  because it wasn't asked for this turn and deserves its own pass (what
  else should be audited, and whether it belongs inside the same
  transaction as the write it's logging).
- `app/workers/base_worker.py` exists but no page uses it yet — logging in
  still runs on whatever thread calls `AuthService.login`.
- No database migration seed data for inventory entities, no multi-tenant
  row-level security, no read replicas or connection pooling tuning
  (`app/database/session.py`'s engine is the SQLAlchemy default pool,
  unconfigured for production load).
