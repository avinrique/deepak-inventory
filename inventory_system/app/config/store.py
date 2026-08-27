"""Reads and writes the per-user config file the setup wizard produces.

Why this exists at all: a packaged .exe cannot carry the database
credentials -- a PyInstaller bundle is trivially unpacked, and every
customer would end up holding the same password. So the binary ships with
no credentials and the admin supplies them once, per machine, through
app.ui.setup_wizard; this module is where that answer is persisted.

Layout of config.json (app/core/paths.py::config_file):

    {
      "database_url": "postgresql+psycopg://user@host:5432/db?sslmode=require",
      "database_password": "<base64 ciphertext>",
      "session_idle_timeout_minutes": 30
    }

The password is deliberately *not* part of database_url on disk. It is
encrypted separately with Windows DPAPI (CryptProtectData), which keys the
ciphertext to the logged-in Windows user account: another user on the same
machine, or anyone who copies the file elsewhere, cannot decrypt it, and no
key material of ours has to be stored anywhere. load() reassembles the full
URL in memory.

Off Windows there is no DPAPI, so the password is stored obfuscated but not
encrypted, the file is chmod 0600, and a warning is logged. That path is for
development only -- the shipped product is Windows.
"""
import base64
import ctypes
import json
import logging
import os
import sys
from typing import Any

from sqlalchemy.engine import URL, make_url

from app.core.paths import config_file

_logger = logging.getLogger(__name__)

# Ties a ciphertext to this application, so a blob produced by some other
# program on the same account cannot be fed to us and vice versa. Not a
# secret (it ships in the binary); it is a namespace, not a key.
_ENTROPY = b"InventoryManagementSystem/config/v1"

_PASSWORD_KEY = "database_password"
_URL_KEY = "database_url"


class ConfigError(Exception):
    """config.json exists but cannot be used (unreadable, malformed, or
    encrypted for a different Windows account)."""


# -- DPAPI ---------------------------------------------------------------- #
if sys.platform == "win32":
    # Imported here, not at module scope: ctypes.wintypes is a
    # Windows-only module and importing it elsewhere can fail outright.
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _to_blob(data: bytes) -> "_Blob":
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: "_Blob") -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(blob.pbData)


def secrets_are_encrypted() -> bool:
    """False means the stored password is only obfuscated (non-Windows dev
    machines). Surfaced in the setup wizard so nobody is misled about it.
    """
    return sys.platform == "win32"


def _encrypt(plaintext: str) -> str:
    raw = plaintext.encode("utf-8")
    if not secrets_are_encrypted():
        _logger.warning("DPAPI is unavailable on %s - the database password in %s is "
                        "obfuscated, not encrypted. Development only.",
                        sys.platform, config_file())
        return base64.b64encode(raw).decode("ascii")

    out = _Blob()
    ok = _crypt32.CryptProtectData(ctypes.byref(_to_blob(raw)), None,
                                   ctypes.byref(_to_blob(_ENTROPY)), None, None,
                                   CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise ConfigError(
            f"Windows could not encrypt the database password "
            f"(error {ctypes.get_last_error()}).")
    return base64.b64encode(_from_blob(out)).decode("ascii")


def _decrypt(stored: str) -> str:
    raw = base64.b64decode(stored.encode("ascii"))
    if not secrets_are_encrypted():
        return raw.decode("utf-8")

    out = _Blob()
    ok = _crypt32.CryptUnprotectData(ctypes.byref(_to_blob(raw)), None,
                                     ctypes.byref(_to_blob(_ENTROPY)), None, None,
                                     CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        # Overwhelmingly the "copied config.json from another PC or another
        # Windows account" case: the ciphertext is fine, this user just is
        # not the one it was sealed to. Re-running the wizard fixes it.
        raise ConfigError(
            "The saved database password could not be decrypted on this Windows "
            "account. Re-enter it in Settings -> Database.")
    return _from_blob(out).decode("utf-8")


# -- file I/O ------------------------------------------------------------- #
def exists() -> bool:
    return config_file().is_file()


def load() -> dict[str, Any]:
    """Settings-field-name -> value, with database_url's password put back.

    Returns {} when no config file exists yet (a fresh install, which the
    caller answers by running the setup wizard). Raises ConfigError only
    when a file *is* there but cannot be used -- never silently falls back
    to defaults, because "connected to the wrong database" is worse than
    "refused to start".
    """
    path = config_file()
    if not path.is_file():
        return {}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    if not isinstance(values, dict):
        raise ConfigError(f"{path} does not contain a configuration object.")

    password = values.pop(_PASSWORD_KEY, None)
    url = values.get(_URL_KEY)
    if password and url:
        try:
            values[_URL_KEY] = make_url(url).set(password=_decrypt(password)) \
                .render_as_string(hide_password=False)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - a malformed URL is a config error
            raise ConfigError(f"{path} has an unusable database_url: {exc}") from exc
    return values


def save(values: dict[str, Any]) -> None:
    """Writes config.json, splitting the database password out of the URL.

    Written to a temporary file and then renamed, so an interrupted save
    (or a full disk) leaves the previous good config in place rather than a
    half-written one the app would refuse to start from.
    """
    values = dict(values)
    url = values.get(_URL_KEY)
    if url:
        parsed = make_url(url)
        if parsed.password:
            values[_PASSWORD_KEY] = _encrypt(parsed.password)
            # Rebuilt rather than parsed.set(password=None): URL.set() skips
            # arguments that are None, so it would leave the password in place
            # and write it to disk in clear text.
            values[_URL_KEY] = URL.create(
                drivername=parsed.drivername, username=parsed.username,
                password=None, host=parsed.host, port=parsed.port,
                database=parsed.database, query=parsed.query,
            ).render_as_string(hide_password=False)

    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Best-effort on Windows (where DPAPI is the real protection); the
    # meaningful guard on macOS/Linux dev machines.
    try:
        os.chmod(temp, 0o600)
    except OSError:  # pragma: no cover - filesystem without permission bits
        pass
    os.replace(temp, path)
    _logger.info("Saved configuration to %s", path)


def redacted_url(url: str) -> str:
    """For logs and error dialogs -- never print a password."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - only ever used for display
        return "<unparseable database url>"
