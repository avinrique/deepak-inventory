"""app.config.store — the per-user config file the setup wizard writes.

The assertions that matter here are the security ones: the database
password must never reach disk inside database_url, and load() must
reassemble the URL byte-for-byte (a dropped ?sslmode=require would silently
downgrade a Neon connection to plaintext, and a mangled one would look like
a wrong-password failure to the user).
"""
import json

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
