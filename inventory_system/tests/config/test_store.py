"""app.config.store — the per-user config file the setup wizard writes.

The assertions that matter here are the security ones: the database
password must never reach disk inside database_url, and load() must
reassemble the URL byte-for-byte (a dropped ?sslmode=require would silently
downgrade a Neon connection to plaintext, and a mangled one would look like
a wrong-password failure to the user).
"""
import base64
import json
import sys

import pytest
from sqlalchemy.engine import make_url

from app.config import store

# The password is percent-encoded, as it must be inside any URL: decoded it
# is "s3cr3t p@ss" -- a space and an '@', the characters that break naive
# string splitting of a DSN.
PASSWORD = "s3cr3t p@ss"
URL = ("postgresql+psycopg://neondb_owner:s3cr3t%20p%40ss@ep-x.aws.neon.tech:5432"
       "/neondb?sslmode=require&channel_binding=require")


def assert_same_url(actual: str, expected: str) -> None:
    """Compares the parsed URLs, not the strings: SQLAlchemy sorts query
    parameters when it renders, so ?sslmode=..&channel_binding=.. comes back
    in the other order. That reordering is meaningless; a lost parameter is
    not, and this still catches one.
    """
    left, right = make_url(actual), make_url(expected)
    for part in ("drivername", "username", "password", "host", "port", "database"):
        assert getattr(left, part) == getattr(right, part), part
    assert dict(left.query) == dict(right.query)


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(store, "config_file", lambda: path)
    return path


def test_password_never_appears_in_the_saved_url(config_path):
    store.save({"database_url": URL})

    raw = config_path.read_text(encoding="utf-8")
    assert PASSWORD not in raw
    assert "s3cr3t%20p%40ss" not in raw
    stored = json.loads(raw)["database_url"]
    assert make_url(stored).password is None
    assert_same_url(stored, "postgresql+psycopg://neondb_owner@ep-x.aws.neon.tech:5432"
                            "/neondb?sslmode=require&channel_binding=require")


def test_load_reassembles_the_exact_url_that_was_saved(config_path):
    store.save({"database_url": URL})

    # Round-trips the password *and* both query parameters, including a
    # password whose decoded form contains ' ' and '@'.
    loaded = store.load()["database_url"]
    assert_same_url(loaded, URL)
    assert make_url(loaded).password == PASSWORD


def test_non_secret_settings_round_trip_untouched(config_path):
    store.save({"database_url": URL, "session_idle_timeout_minutes": 45,
                "pg_bin_dir": r"C:\PostgreSQL\bin"})

    loaded = store.load()
    assert loaded["session_idle_timeout_minutes"] == 45
    assert loaded["pg_bin_dir"] == r"C:\PostgreSQL\bin"
    assert "database_password" not in loaded  # folded back into the URL


def test_a_url_without_a_password_stores_no_password_key(config_path):
    store.save({"database_url": "postgresql+psycopg://localhost:5432/inventory"})

    assert "database_password" not in json.loads(config_path.read_text())


def test_missing_config_file_reads_as_empty_not_as_an_error(config_path):
    """A fresh install. The caller answers this with the setup wizard."""
    assert store.load() == {}


def test_malformed_config_raises_rather_than_falling_back_to_defaults(config_path):
    config_path.write_text("{not json", encoding="utf-8")

    # Silently defaulting here would point a till at some other database.
    with pytest.raises(store.ConfigError):
        store.load()


def test_config_containing_a_bare_list_is_rejected(config_path):
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(store.ConfigError):
        store.load()


def test_save_replaces_atomically_and_leaves_no_temp_file(config_path):
    store.save({"database_url": URL})
    store.save({"database_url": URL, "session_idle_timeout_minutes": 5})

    siblings = {p.name for p in config_path.parent.iterdir()}
    assert siblings == {"config.json"}


def test_redacted_url_hides_the_password():
    assert "s3cr3t" not in store.redacted_url(URL)


def test_redacted_url_survives_an_unparseable_value():
    assert store.redacted_url("!!! not a url !!!") == "<unparseable database url>"


# -- Windows DPAPI --------------------------------------------------------- #
# These only run on Windows, which is the point: the encryption path is
# Windows-only, so on any other machine it is never executed at all and a
# defect in it cannot be observed. Development happens on macOS, so CI is the
# first and only place this code runs.

pytestmark_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is Windows-only")


@pytestmark_windows
def test_dpapi_round_trips_a_password():
    """Catches the class of ctypes bug that does not raise: a DATA_BLOB holds
    only a length and a raw pointer, so if the buffer behind it is garbage
    collected the API reads freed memory and returns plausible-looking
    rubbish rather than failing."""
    secret = "correct horse battery staple"

    assert store._decrypt(store._encrypt(secret)) == secret


@pytestmark_windows
def test_dpapi_handles_non_ascii_and_long_passwords():
    secret = "pässwörd-ünïcode-" + ("x" * 500)

    assert store._decrypt(store._encrypt(secret)) == secret


@pytestmark_windows
def test_dpapi_ciphertext_differs_from_the_plaintext():
    """A base64 of the plaintext would round-trip perfectly too — this is
    what distinguishes real encryption from the development fallback."""
    secret = "not-really-encrypted"

    encoded = store._encrypt(secret)

    assert base64.b64decode(encoded) != secret.encode()
    assert secret not in encoded


@pytestmark_windows
def test_dpapi_rejects_a_blob_sealed_with_different_entropy(monkeypatch):
    """The entropy namespaces our ciphertext, so a blob another program on
    the same account produced cannot be fed to us."""
    encoded = store._encrypt("secret")
    monkeypatch.setattr(store, "_ENTROPY", b"some-other-application")

    with pytest.raises(store.ConfigError):
        store._decrypt(encoded)


@pytestmark_windows
def test_repeated_encryption_does_not_exhaust_or_corrupt_the_heap():
    """LocalFree is called with a full-width pointer. Given ctypes' default
    marshalling truncates an untyped pointer to 32 bits on 64-bit Windows,
    getting this wrong corrupts the heap — which shows up as a crash
    somewhere unrelated, so it is worth hammering deliberately."""
    for index in range(200):
        assert store._decrypt(store._encrypt(f"password-{index}")) == f"password-{index}"
