# Deploying Inventory Management System on Windows

This is the operational guide: how a build is produced, what the installer
does, how an installed copy finds its database, and where to look when
something goes wrong. For the internal design, see `architecture.md`.

## What a user receives

A single file, `InventoryManagementSystemSetup-<version>.exe`. It carries
its own Python runtime, Qt, the PostgreSQL client tools and every library —
the machine needs none of them installed, and no development environment.

Requirements: 64-bit Windows 10 or 11, and network access to a PostgreSQL
database. About 400 MB of disk.

## Producing a build

### Via CI (the reference build)

`.github/workflows/windows-build.yml` runs on every push to `main` and on
every `v*` tag. It runs the tests, builds the executable, **runs the built
executable's self-test**, captures screenshots at each Windows scaling
factor, and builds the installer. Download the `installer` artifact from the
run; tagged builds are also attached to a GitHub release.

Development happens on macOS, where a Windows executable cannot be produced
at all — PyInstaller does not cross-compile and Inno Setup is Windows-only —
so CI is where the artefact a customer receives is made and verified.

### On a Windows machine

```powershell
cd inventory_system
.\packaging\build_windows.ps1
```

Needs Python 3.12 and, for the installer step, [Inno Setup 6][inno].
Produces `dist\InventoryManagementSystem\` and
`dist\installer\InventoryManagementSystemSetup-<version>.exe`.

[inno]: https://jrsoftware.org/isdl.php

### Cutting a release

1. Bump `VERSION` in `app/__version__.py` (plain `major.minor.patch` — the
   Windows version resource cannot represent a `-rc1` suffix).
2. Commit, tag `vX.Y.Z`, push the tag.
3. CI builds and attaches the installer to the release.

The installer's `AppId` GUID is fixed, so a later version upgrades an
existing installation in place rather than installing alongside it.

## Installing

The installer offers the installation directory, always creates a Start Menu
entry, and offers a desktop shortcut. It does not require administrator
rights: a user without them can install into their own profile.

Uninstalling removes the program but **keeps** the configuration and logs, so
an upgrade — which uninstalls before it reinstalls — does not send every
machine back to the setup wizard.

## First run

On first launch the application has no database configured, and shows the
setup wizard:

1. **Connection.** Server, port, database, username, password and encryption
   mode — or paste a connection link and let it fill the fields in. **Test
   Connection** must succeed before Continue is enabled, so details that were
   never verified cannot be saved.
2. **Set up this database** appears only when the database is empty. It runs
   the migrations, seeds the role and permission catalogue, and asks for the
   first administrator account. That account is the Owner; every other user
   is created from Users once it can log in.

Reachable afterwards from Settings, for moving an installation to a different
server.

Both a cloud database (Neon, RDS, Azure) and a local or LAN PostgreSQL work.
For a cloud database keep encryption on **Require**.

## Where things are kept

| What | Where |
|---|---|
| Connection settings | `%APPDATA%\InventoryManagementSystem\config.json` |
| Logs | `%LOCALAPPDATA%\InventoryManagementSystem\logs\` |
| Backups | `%LOCALAPPDATA%\InventoryManagementSystem\backups\` |
| Program | `C:\Program Files\InventoryManagementSystem\` (or the chosen directory) |

Nothing writable is kept in the installation directory: a standard user
cannot write there, and an upgrade replaces it.

**The database password is not stored in plain text.** `config.json` holds
the connection URL with the password removed, plus the password encrypted
with Windows DPAPI — which keys it to the Windows account that entered it, so
copying the file to another machine or another user account does not carry
the password with it. If that happens the application says so and asks for it
again rather than failing with a confusing authentication error.

## Deploying a fixed configuration

To roll out one connection to many machines without anyone using the wizard,
set environment variables — they override `config.json`:

| Variable | Purpose |
|---|---|
| `INVENTORY_DATABASE_URL` | `postgresql+psycopg://user:password@host:5432/db?sslmode=require` |
| `INVENTORY_DB_CONNECT_TIMEOUT` | Seconds before an unreachable server gives up (default 10) |
| `INVENTORY_SESSION_IDLE_TIMEOUT_MINUTES` | Idle logout (default 30) |
| `INVENTORY_LOG_DIR` | Alternative log directory |
| `INVENTORY_BACKUP_DIR` | Alternative backup directory |
| `INVENTORY_PG_BIN_DIR` | Where `pg_dump`/`pg_restore` live, if not the bundled copy |

A machine-wide variable puts the password in the registry in clear text; the
setup wizard is the more secure option where it is practical.

Alternatively, initialise a database from a command line:

```powershell
$env:INVENTORY_DATABASE_URL = "postgresql+psycopg://..."
python scripts\init_db.py --create-owner
```

## Backup and restore

Settings → Backup runs `pg_dump`, verifies the result with
`pg_restore --list`, and records it. The two programs are installed with the
application, so this works on a machine with no PostgreSQL of its own. The
password is passed to them through the environment, never on the command
line where other processes could read it.

Restoring **replaces all current data** and asks for typed confirmation.

## When something goes wrong

The log is the first place to look:
`%LOCALAPPDATA%\InventoryManagementSystem\logs\inventory_system.log`. It
opens with the version, build commit, Python and Qt versions, OS, and the
screen geometry and DPI of every display — enough to reproduce a report
without another round of questions. It rotates at 2 MB, keeping five files.
Any unexpected error offers an **Open Log Folder** button.

| What the user sees | What it means |
|---|---|
| "The database server could not be reached" | Network, wrong host, or a blocked port. |
| "The database rejected the username or password" | Credentials. Settings → Database. |
| "That database does not exist on the server" | Database name. Settings → Database. |
| "The database did not respond in time" | A suspended cloud instance or a slow link; retry. |
| "Database needs updating" | The database is behind this version of the application. An administrator should run the newer installer on the machine that manages it, or `scripts\init_db.py`. |
| "This installation is incomplete" | Files missing from the installation. Reinstall. |

These are deliberately distinct. An earlier version reported every
connection failure as a schema problem, which sent people looking for a
migration to run when their network was down.
