"""app.backup.postgres_backup — the pg_dump/pg_restore subprocess wrapper.

Tests that don't need a database (sanitization, filename collision
avoidance, connection-string parsing, missing-tool handling) run always.
Tests that actually shell out to pg_dump/pg_restore are gated on the
``live_db`` fixture (tests/conftest.py) and, critically, always pass
``settings.test_database_url`` explicitly to create_backup/restore_backup
— never the app's real database_url — so there is no path by which
running this file could touch or restore over real business data. See
tests/conftest.py's docstring for the same safety rule applied to every
other live-DB test in this suite.
"""
import hashlib
from pathlib import Path

import pytest

from app.backup.postgres_backup import (
    BackupToolNotFoundError,
    _connection_params,
    _require_tool,
    _sanitize_output,
    _unique_backup_path,
    create_backup,
    restore_backup,
    verify_backup_file,
)
from app.config.settings import settings
from app.database.session import get_session
from app.models import Organization


# -- sanitization ------------------------------------------------------- #

def test_sanitize_output_redacts_connection_url():
    text = "connection to postgresql://myuser:sekret@dbhost:5432/inventory failed: refused"
    result = _sanitize_output(text)
    assert "sekret" not in result
    assert "myuser" not in result
    assert "[REDACTED]" in result


def test_sanitize_output_redacts_password_key_value():
    text = "FATAL: password authentication failed for user \"x\" password=hunter2"
    result = _sanitize_output(text)
    assert "hunter2" not in result
    assert "[REDACTED]" in result


def test_sanitize_output_leaves_ordinary_errors_untouched():
    text = "pg_dump: error: connection to server was lost"
    assert _sanitize_output(text) == text


# -- connection parsing --------------------------------------------------#

def test_connection_params_parses_full_url():
    params = _connection_params("postgresql+psycopg://alice:s3cret@dbhost:6543/mydb")
    assert params.host == "dbhost"
    assert params.port == 6543
    assert params.user == "alice"
    assert params.password == "s3cret"
    assert params.database == "mydb"


def test_connection_params_defaults_missing_host_and_port():
    params = _connection_params("postgresql+psycopg://alice@/mydb")
    assert params.host == "localhost"
    assert params.port == 5432
    assert params.password is None


# -- no-overwrite filename collision avoidance --------------------------- #

def test_unique_backup_path_returns_candidate_when_free(tmp_path):
    path = _unique_backup_path(tmp_path, "inventory_20260101_000000.dump")
    assert path == tmp_path / "inventory_20260101_000000.dump"


def test_unique_backup_path_appends_numeric_suffix_on_collision(tmp_path):
    (tmp_path / "inventory_20260101_000000.dump").touch()
    path = _unique_backup_path(tmp_path, "inventory_20260101_000000.dump")
    assert path == tmp_path / "inventory_20260101_000000_1.dump"

    path.touch()
    path2 = _unique_backup_path(tmp_path, "inventory_20260101_000000.dump")
    assert path2 == tmp_path / "inventory_20260101_000000_2.dump"


def test_create_backup_never_overwrites_an_existing_file(tmp_path, live_db, monkeypatch):
    import app.backup.postgres_backup as backup_module

    monkeypatch.setattr(backup_module, "format_backup_filename",
                        lambda *_args, **_kwargs: "fixed_name.dump")

    first = create_backup(settings.test_database_url, str(tmp_path))
    second = create_backup(settings.test_database_url, str(tmp_path))

    assert first.success and second.success
    assert first.filename != second.filename
    assert Path(first.file_path).exists()
    assert Path(second.file_path).exists()


# -- missing tool ---------------------------------------------------------#

def test_require_tool_raises_when_not_on_path(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(BackupToolNotFoundError):
        _require_tool("pg_dump")


# -- verify_backup_file (no live DB needed — reads the file directly) -----#

def test_verify_backup_file_false_for_missing_file(tmp_path):
    assert verify_backup_file(str(tmp_path / "does_not_exist.dump")) is False


def test_verify_backup_file_false_for_garbage_file(tmp_path):
    garbage = tmp_path / "garbage.dump"
    garbage.write_bytes(b"not a real pg_dump file")
    assert verify_backup_file(str(garbage)) is False


# -- create_backup / restore_backup end-to-end (live_db only) ------------#

def test_create_backup_produces_a_verified_dump_with_correct_checksum(tmp_path, live_db):
    outcome = create_backup(settings.test_database_url, str(tmp_path))

    assert outcome.success is True
    assert outcome.verified is True
    assert outcome.error_message is None
    assert Path(outcome.file_path).exists()
    assert outcome.file_size_bytes == Path(outcome.file_path).stat().st_size

    recomputed = hashlib.sha256(Path(outcome.file_path).read_bytes()).hexdigest()
    assert outcome.checksum_sha256 == recomputed


def test_create_backup_with_unreachable_host_fails_safely_without_leaking_password(
        tmp_path, live_db):
    bad_url = "postgresql+psycopg://baduser:sup3rsecret@127.0.0.1:1/inventory_test"
    outcome = create_backup(bad_url, str(tmp_path))

    assert outcome.success is False
    assert outcome.verified is False
    assert outcome.error_message is not None
    assert "sup3rsecret" not in outcome.error_message
    # No partial/empty dump file left behind for a failed attempt.
    assert list(tmp_path.iterdir()) == []


def test_restore_backup_reverts_the_database_to_the_backed_up_state(tmp_path, live_db):
    with get_session() as session:
        session.add(Organization(name="Before Backup"))

    outcome = create_backup(settings.test_database_url, str(tmp_path))
    assert outcome.success

    with get_session() as session:
        session.add(Organization(name="Added After Backup"))
        assert session.query(Organization).count() == 2

    restore_outcome = restore_backup(settings.test_database_url, outcome.file_path)
    assert restore_outcome.success is True
    assert restore_outcome.error_message is None

    with get_session() as session:
        names = [o.name for o in session.query(Organization).all()]
        assert names == ["Before Backup"]


def test_restore_backup_missing_file_fails_safely(live_db):
    outcome = restore_backup(settings.test_database_url, "/nonexistent/path/backup.dump")
    assert outcome.success is False
    assert outcome.error_message is not None
