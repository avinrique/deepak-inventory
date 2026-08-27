"""app.config.settings — the resolution order, which is a security and a
correctness boundary.

An IT department rolling this out sets INVENTORY_* variables by policy; a
shop admin answers the setup wizard, which writes config.json. If the file
could win over the environment, the policy would be silently unenforceable.
And if a relative log/backup directory could survive, we would be back to
resolving paths against a CWD the Start Menu chose for us.
"""
import os

import pytest

from app.config import settings as settings_module
from app.config.settings import Settings
from app.core.paths import data_dir


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """No real config.json, no inherited INVENTORY_* vars, and the shared
    ``settings`` object restored afterwards.

    That last part matters: reload_settings() deliberately mutates the
    module-global in place, so without a snapshot one test here would leave
    every later test in the session pointed at a temp-directory config.
    """
    monkeypatch.setattr(settings_module.store, "config_file",
                        lambda: tmp_path / "config.json")
    for key in [k for k in os.environ if k.startswith("INVENTORY_")]:
        monkeypatch.delenv(key, raising=False)

    shared = settings_module.settings
    snapshot = {name: getattr(shared, name) for name in type(shared).model_fields}
    previous_error = settings_module.config_error
    try:
        yield
    finally:
        for name, value in snapshot.items():
            object.__setattr__(shared, name, value)
        settings_module.config_error = previous_error


def _settings(**file_values) -> Settings:
    if file_values:
        settings_module.store.save(file_values)
    return Settings(_env_file=None, **settings_module._file_values())


def test_config_file_supplies_the_database_url(tmp_path):
    result = _settings(database_url="postgresql+psycopg://a:b@filehost/db")

    assert result.database_url == "postgresql+psycopg://a:b@filehost/db"
    assert result.is_configured()


def test_environment_variable_beats_the_config_file(monkeypatch):
    """The deployment override. pydantic treats constructor arguments as the
    highest-priority source, so _file_values() must drop any key the
    environment already sets — otherwise the file would win instead."""
    monkeypatch.setenv("INVENTORY_DATABASE_URL", "postgresql+psycopg://a:b@envhost/db")

    result = _settings(database_url="postgresql+psycopg://a:b@filehost/db")

    assert "envhost" in result.database_url


def test_an_unconfigured_install_reports_itself_rather_than_guessing():
    """There is deliberately no working default. A fresh install must land
    in the setup wizard, not connect to some arbitrary localhost database."""
    result = _settings()

    assert result.database_url == ""
    assert result.is_configured() is False


def test_a_relative_log_dir_is_anchored_to_user_data_not_the_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("INVENTORY_LOG_DIR", "logs")
    monkeypatch.chdir(tmp_path)

    result = _settings()

    assert result.log_dir == str(data_dir() / "logs")


def test_an_absolute_log_dir_is_left_alone(monkeypatch):
    monkeypatch.setenv("INVENTORY_LOG_DIR", "/var/log/inventory")

    assert _settings().log_dir == "/var/log/inventory"


def test_a_broken_config_file_is_reported_not_silently_ignored(tmp_path):
    (tmp_path / "config.json").write_text("{ broken", encoding="utf-8")

    values = settings_module._file_values()

    # Import must not explode, but the failure has to be visible so app.main
    # can show it — falling through to defaults would be the dangerous outcome.
    assert values == {}
    assert settings_module.config_error is not None


def test_reload_settings_mutates_the_shared_object_in_place(tmp_path):
    """Forty modules did `from app.config.settings import settings` and hold
    a reference; rebinding the global would strand every one of them on the
    stale copy after the wizard saves."""
    original = settings_module.settings
    settings_module.store.save({"database_url": "postgresql+psycopg://a:b@newhost/db"})

    returned = settings_module.reload_settings()

    assert returned is original
    assert "newhost" in original.database_url
