"""Central settings.

Resolution order, highest priority first:

1. ``INVENTORY_*`` environment variables -- the escape hatch for an IT
   department rolling the app out with a machine policy or a login script.
2. ``config.json`` in the user's AppData -- what the first-run setup wizard
   writes, and what a normal install actually runs on (app.config.store).
3. ``inventory_system/.env`` -- development only. Located absolutely, from
   the project directory, and skipped entirely in a frozen build so a stray
   .env sitting next to the .exe can never redirect a customer's app.
4. The defaults below, which are placeholders and never real credentials.

Nothing here is ever resolved against the current working directory: a .exe
launched from a Start Menu shortcut has no meaningful CWD, and the old
``env_file=".env"`` meant such a launch silently found no config at all and
fell back to localhost.
"""
import logging
import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import store
from app.core.paths import backups_dir, data_dir, is_frozen, logs_dir, resource_root

_logger = logging.getLogger(__name__)

# Development convenience only -- see the module docstring. resource_root()
# is the project directory when not frozen.
_DEV_ENV_FILE = None if is_frozen() else resource_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVENTORY_", env_file=_DEV_ENV_FILE,
                                      env_file_encoding="utf-8", extra="ignore")

    # No default that could ever silently "work": an unconfigured install
    # must fail into the setup wizard, not quietly connect somewhere.
    database_url: str = ""

    # Seconds to wait for a TCP connection before giving up. Without this,
    # an unreachable host blocks on the OS timeout (~2 minutes on Windows)
    # with the splash screen up and no way to cancel.
    db_connect_timeout: int = 10

    log_dir: str = Field(default_factory=lambda: str(logs_dir()))
    backup_dir: str = Field(default_factory=lambda: str(backups_dir()))

    # Where pg_dump/pg_restore live, when not the copy shipped in the
    # install directory and not on PATH. See app.backup.postgres_backup.
    pg_bin_dir: str | None = None

    session_idle_timeout_minutes: int = 30

    # Failed-login lockout (see AuthService's module docstring): after this
    # many failed attempts for the same email within login_lockout_window_
    # minutes, further attempts are refused for login_lockout_duration_
    # minutes. Deployment-wide (not per-organization, unlike session
    # timeout/password policy) because lockout has to apply *before* which
    # organization a login even belongs to is known.
    login_lockout_threshold: int = 5
    login_lockout_window_minutes: int = 15
    login_lockout_duration_minutes: int = 15

    # Deliberately separate from database_url, and unset by default: DB
    # integration tests (tests/models, tests/repositories/test_sql_*) skip
    # entirely unless this is explicitly set, so there is no way for the
    # test suite to accidentally create_all()/drop_all() against whatever
    # real, possibly-production database database_url happens to point at.
    test_database_url: str | None = None

    @field_validator("log_dir", "backup_dir")
    @classmethod
    def _anchor_relative_dirs(cls, value: str) -> str:
        """Turns a relative directory into an absolute one under the user's
        data directory.

        A relative value is legitimate (a developer's .env says ``logs``),
        but resolving it against the CWD is not: the same setting would mean
        the project directory when run from a terminal and something like
        C:\\Windows\\system32 -- unwritable -- when run from a shortcut.
        Anchoring instead of rejecting keeps existing .env files working.
        """
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else data_dir() / path)

    def is_configured(self) -> bool:
        """False on a fresh install -- app.main answers it with the setup
        wizard rather than a connection error nobody can act on."""
        return bool(self.database_url.strip())


def _file_values() -> dict:
    """config.json, minus anything an environment variable already sets.

    Filtering here is what keeps the documented precedence: pydantic treats
    constructor arguments as the *highest* priority source, so passing a
    config.json value for a field that also has an INVENTORY_* variable set
    would let the file silently override the environment.
    """
    try:
        values = store.load()
    except store.ConfigError as exc:
        # Surfaced by app.main as a real dialog. Raising here instead would
        # produce a bare traceback at import time, before there is any UI.
        global config_error
        config_error = exc
        _logger.error("Ignoring unusable configuration file: %s", exc)
        return {}
    return {key: value for key, value in values.items()
            if f"INVENTORY_{key.upper()}" not in os.environ}


config_error: store.ConfigError | None = None
settings = Settings(**_file_values())


def reload_settings() -> Settings:
    """Re-reads every source into the existing ``settings`` object.

    Mutates in place rather than rebinding the module global, because forty
    call sites did ``from app.config.settings import settings`` and hold a
    reference to the original object; rebinding would leave all of them on
    the stale copy. Called after the setup wizard saves, and paired with
    app.database.session.reset_engine().
    """
    global config_error
    config_error = None
    fresh = Settings(**_file_values())
    for name in type(fresh).model_fields:
        object.__setattr__(settings, name, getattr(fresh, name))
    return settings
