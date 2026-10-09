# Deploying Inventory Management System on Windows

This is the operational guide: how a build is produced, what the installer
does, how an installed copy finds its database, and where to look when
something goes wrong. For the internal design, see `architecture.md`.

## What a user receives

A single file, `InventoryManagementSystemSetup-<version>.exe`. It carries
its own Python runtime, Qt, a complete PostgreSQL server and every library —
the machine needs none of them installed, and no development environment.

Requirements: 64-bit Windows 10 or 11. Roughly 450 MB of disk for the
program, plus whatever the database grows to. **No database server and no
internet connection are required**: the installer brings its own PostgreSQL,
and the default choice on first run keeps everything on the machine.
Connecting to an existing server, on the LAN or in the cloud, remains an
option — it is just no longer a prerequisite.

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

1. **Where should your data be stored?**
   - **On this computer** — the recommended answer, and the one that needs no
     input. The application initialises a PostgreSQL cluster of its own under
     `%LOCALAPPDATA%`, starts it on a free port bound to `127.0.0.1`, and
     creates the database. Nothing is typed, nothing is signed up for, and
     nothing leaves the machine. The first run takes about a minute.
   - **On a server or cloud database** — for a PostgreSQL that already exists,
     continuing to step 2.
2. **Connection** (only for the second choice). Server, port, database,
   username, password and encryption mode — or paste a connection link and let
   it fill the fields in. **Test Connection** must succeed before Continue is
   enabled, so details that were never verified cannot be saved.
3. **Set up this database** appears only when the database is empty — for the
   local choice, always. It runs the migrations, seeds the role and permission
   catalogue, and asks for the first administrator account. That account is the
   Owner; every other user is created from Users once it can log in.

Both routes converge at step 3, so an empty database is prepared the same way
whichever was chosen.

Reachable afterwards from the startup error dialog's **Database Settings…**,
for moving an installation to a different server or onto this computer.

A cloud database (Neon, RDS, Azure) and a LAN PostgreSQL both still work. For
a cloud database keep encryption on **Require**.

### The built-in database

Only relevant to installations that chose "on this computer".

- The server runs only while the application does. It is started during
  startup and shut down on exit; it is **not** a Windows service, because the
  installer asks for no administrator rights and registering a service needs
  them.
- It listens on `127.0.0.1` only. No other machine can reach it, and Windows
  Firewall has nothing to prompt about. A second PC needs the remote option,
  pointed at a real server.
- `5432` is used when free; otherwise the next free port upwards. The chosen
  port is part of the saved connection URL, so it is remembered.
- The superuser password is generated, never shown, and stored the same way a
  typed one is — DPAPI-encrypted in `config.json`.
- Closing the application from Task Manager leaves the server running. The
  next launch finds it and reuses it rather than failing.
- **Upgrading the application never touches the data.** `pgdata` lives outside
  the installation directory for exactly this reason. The installer does stop
  a running server before replacing files, because a live `postgres.exe` locks
  its own binary.
- **Uninstalling does not delete the database either.** There is no way for an
  uninstaller to tell "upgrading" from "removing for good", and erasing a
  shop's records on a wrong guess is not a risk worth taking. Delete
  `%LOCALAPPDATA%\InventoryManagementSystem\pgdata` by hand if that is really
  what is wanted — take a backup first.
- A release that bundles a **different PostgreSQL major version** cannot open
  an existing cluster; PostgreSQL enforces that itself. The application
  detects it and refuses to start with an explanation rather than failing
  obscurely. This is why `POSTGRES_MAJOR` in the build workflow is pinned and
  why changing it is treated as a migration.

## Where things are kept

| What | Where |
|---|---|
| Connection settings | `%APPDATA%\InventoryManagementSystem\config.json` |
| The built-in database | `%LOCALAPPDATA%\InventoryManagementSystem\pgdata\` |
| Logs (including the server's) | `%LOCALAPPDATA%\InventoryManagementSystem\logs\` |
| Backups | `%LOCALAPPDATA%\InventoryManagementSystem\backups\` |
| Program | `C:\Program Files\InventoryManagementSystem\` (or the chosen directory) |
| Bundled PostgreSQL | `<program>\pgsql\` |

`pgdata` is under `%LOCALAPPDATA%` rather than `%APPDATA%` deliberately: a
roaming profile would try to copy an entire live database between machines.
It is also the one directory here that is irreplaceable — logs and backups can
be regenerated, `pgdata` *is* the business's records.

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
| `INVENTORY_DATABASE_MANAGED_LOCALLY` | `1` to use the built-in database without anyone answering the wizard. Leave unset (or `0`) for a server someone else administers — the application would otherwise try to start a database it never created. |
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
| "The built-in database cannot be used while this program is running as an administrator" | PostgreSQL refuses an elevated token — its own rule. Close the application and open it normally, from the Start Menu or the desktop shortcut, rather than via "Run as administrator". |
| "This copy of the application does not include the built-in database" | The installer was built without `packaging/fetch_pgserver.py` having run. Reinstall from a proper release, or use a server instead. |
| "...was created with PostgreSQL *N*, but this version includes PostgreSQL *M*" | The release bundles a different major version than the existing data was made with. Reinstall the previous version, back up from Settings → Backup, then upgrade and restore. The data is not damaged. |
| "The database on this computer could not be started" | See `pgserver.log` in the log directory — that is PostgreSQL's own output. A stale lock after a hard power-off usually clears on a retry. |
| "This installation is incomplete" | Files missing from the installation. Reinstall. |

These are deliberately distinct. An earlier version reported every
connection failure as a schema problem, which sent people looking for a
migration to run when their network was down.
