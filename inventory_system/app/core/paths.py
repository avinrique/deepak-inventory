"""Every filesystem location the app uses, resolved in one place.

Two rules this module exists to enforce:

1. **Nothing is resolved against the current working directory.** A
   .exe started from a Start Menu shortcut inherits whatever CWD Windows
   feels like giving it, so a relative "logs" or ".env" silently resolves
   somewhere unrelated -- or, under C:\\Program Files, somewhere
   unwritable, which used to kill the app on the first line of main()
   before any window existed to report it.
2. **Read-only resources and writable data live in different trees.** The
   installation directory is read-only for a normal user (and is wiped on
   uninstall/upgrade); config, logs and backups therefore go to the user's
   own AppData, and survive both.

Deliberately Qt-free and dependency-free: configure_logging() and
app.config.settings both call in here *before* QApplication exists, so
QStandardPaths is not available yet.
"""
import os
import sys
from pathlib import Path

from app.__version__ import APP_SLUG


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_root() -> Path:
    """Directory that bundled read-only resources sit under.

    Frozen, that is PyInstaller's extraction root (sys._MEIPASS); in
    development it is the inventory_system/ project directory. Both are
    derived from the interpreter or from __file__, never from the CWD.
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # noqa: SLF001 - PyInstaller's documented API
    # app/core/paths.py -> app/core -> app -> inventory_system/
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """A bundled resource: stylesheets, alembic.ini, migrations/, the icon.

    Every one of these must also be listed in packaging's spec file --
    resolving a path here does not make PyInstaller ship the file.
    """
    return resource_root().joinpath(*parts)


def _windows_dir(env_var: str, fallback: Path) -> Path:
    value = os.environ.get(env_var)
    return Path(value) if value else fallback


def config_dir() -> Path:
    """Roaming, per-user: config.json. Follows the user between machines
    on a domain, which is what you want for "which database do I talk to".
    """
    if sys.platform == "win32":
        return _windows_dir("APPDATA", Path.home() / "AppData" / "Roaming") / APP_SLUG
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_SLUG
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_SLUG


def data_dir() -> Path:
    """Machine-local, per-user: logs and backups. Deliberately not roaming
    -- these are large, disposable, and specific to one machine.
    """
    if sys.platform == "win32":
        return _windows_dir("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_SLUG
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_SLUG
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_SLUG


def config_file() -> Path:
    return config_dir() / "config.json"


def logs_dir() -> Path:
    return data_dir() / "logs"


def backups_dir() -> Path:
    return data_dir() / "backups"


def icon_path() -> Path:
    """The application icon.

    PyInstaller flattens it to the bundle root, but in a source checkout it
    lives with the other packaging inputs — so both are checked rather than
    keeping a duplicate copy of the file at the project root just to make
    one path work.
    """
    bundled = resource_path("app.ico")
    if bundled.is_file():
        return bundled
    return resource_path("packaging", "app.ico")


def pg_bin_dir() -> Path:
    """Where the installer puts pg_dump.exe/pg_restore.exe. Checked before
    PATH by app.backup.postgres_backup, so Backup works on a machine with
    no PostgreSQL installation of its own.
    """
    return resource_path("pgtools")


def ensure_user_dirs() -> None:
    """Create the writable directories. Called once at startup, before
    logging is configured -- so it must not log, and must not raise for a
    directory that already exists.
    """
    for directory in (config_dir(), logs_dir(), backups_dir()):
        directory.mkdir(parents=True, exist_ok=True)
