"""app.core.paths — the one module allowed to decide where files live.

Every assertion here exists because the packaged app failed on it: a
CWD-relative path resolves somewhere unwritable when Windows launches the
.exe from a Start Menu shortcut, and writable data placed in the install
directory is destroyed by the next upgrade.
"""
import os
from pathlib import Path

from app.core import paths


def test_bundled_resources_resolve_regardless_of_working_directory(tmp_path, monkeypatch):
    before = paths.resource_path("app", "ui", "styles", "theme.qss")
    monkeypatch.chdir(tmp_path)

    assert paths.resource_path("app", "ui", "styles", "theme.qss") == before


def test_every_resource_the_app_loads_at_startup_actually_exists():
    """These four are read before or during the first window. Anything
    listed here must also be shipped by packaging's spec file — this test
    is what tells you a rename broke the bundle."""
    for parts in (("app", "ui", "styles", "theme.qss"),
                  ("app", "ui", "styles", "order_form.qss"),
                  ("alembic.ini",),
                  ("migrations", "env.py")):
        assert paths.resource_path(*parts).exists(), parts


def test_user_data_never_lands_inside_the_installation_directory():
    """The install directory is read-only for a standard user and is wiped
    on uninstall; config and logs have to outlive both."""
    resources = paths.resource_root().resolve()
    for writable in (paths.config_dir(), paths.data_dir(), paths.logs_dir(),
                     paths.backups_dir()):
        assert resources not in writable.resolve().parents
        assert writable.resolve() != resources


def test_writable_paths_are_absolute():
    for writable in (paths.config_file(), paths.logs_dir(), paths.backups_dir()):
        assert writable.is_absolute()


def test_windows_uses_appdata_for_config_and_localappdata_for_logs(monkeypatch):
    """Roaming config follows a domain user between machines; logs and
    backups are large and machine-specific, so they stay local."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\Test\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")

    assert paths.config_dir() == Path(r"C:\Users\Test\AppData\Roaming") / "InventoryManagementSystem"
    assert paths.logs_dir() == (
        Path(r"C:\Users\Test\AppData\Local") / "InventoryManagementSystem" / "logs")


def test_windows_falls_back_to_the_profile_when_appdata_is_unset(monkeypatch):
    """Rare, but a service or scheduled-task context can lack it, and
    Path(None) would raise rather than degrade."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    assert paths.config_dir().is_absolute()
    assert paths.config_dir().name == "InventoryManagementSystem"


def test_ensure_user_dirs_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(paths, "logs_dir", lambda: tmp_path / "data" / "logs")
    monkeypatch.setattr(paths, "backups_dir", lambda: tmp_path / "data" / "backups")

    paths.ensure_user_dirs()
    paths.ensure_user_dirs()  # a second launch must not raise

    assert (tmp_path / "cfg").is_dir()
    assert (tmp_path / "data" / "logs").is_dir()
    assert (tmp_path / "data" / "backups").is_dir()


def test_is_frozen_is_false_when_running_from_source():
    assert paths.is_frozen() is False
    assert "INVENTORY" not in os.environ.get("PYTHONHOME", "")
