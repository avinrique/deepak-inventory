"""SqlBackupRepository against a live PostgreSQL database — proves the
row round-trips real data and that update_verification only touches the
verified/verified_at fields.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.database.session import get_session
from app.domain.backup import BackupStatus
from app.models import Organization, User
from app.repositories.sql.backup_repository import SqlBackupRepository


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        admin = User(email="admin@acme.test", username="admin", hashed_password="x",
                    full_name="Admin")
        session.add_all([org, admin])
        session.flush()
        return {"org_id": org.id, "admin_id": admin.id}


def _repo():
    return SqlBackupRepository()


def test_record_completed_backup_round_trips_fields(world):
    repo = _repo()
    created = repo.record(
        organization_id=world["org_id"], created_by=world["admin_id"],
        filename="inventory_20260101_000000.dump", file_path="/backups/inventory_x.dump",
        file_size_bytes=12345, checksum_sha256="abc123", status=BackupStatus.COMPLETED,
        verified=True, error_message=None, completed_at=datetime.now(timezone.utc))

    assert created.filename == "inventory_20260101_000000.dump"
    assert created.status == BackupStatus.COMPLETED
    assert created.verified is True
    assert created.verified_at is not None
    assert created.file_size_bytes == 12345
    assert created.checksum_sha256 == "abc123"
    assert created.organization_id == world["org_id"]
    assert created.created_by == world["admin_id"]


def test_record_failed_backup_stores_error_message(world):
    repo = _repo()
    created = repo.record(
        organization_id=world["org_id"], created_by=world["admin_id"],
        filename="inventory_20260101_000001.dump", file_path="/backups/inventory_y.dump",
        file_size_bytes=None, checksum_sha256=None, status=BackupStatus.FAILED,
        verified=False, error_message="pg_dump: connection refused",
        completed_at=datetime.now(timezone.utc))

    assert created.status == BackupStatus.FAILED
    assert created.verified is False
    assert created.verified_at is None
    assert created.error_message == "pg_dump: connection refused"


def test_get_by_id_returns_none_when_missing(world):
    assert _repo().get_by_id(uuid.uuid4()) is None


def test_list_all_orders_newest_first(world):
    repo = _repo()
    first = repo.record(organization_id=world["org_id"], created_by=world["admin_id"],
                        filename="a.dump", file_path="/backups/a.dump",
                        file_size_bytes=1, checksum_sha256="a", status=BackupStatus.COMPLETED,
                        verified=True, error_message=None,
                        completed_at=datetime.now(timezone.utc))
    second = repo.record(organization_id=world["org_id"], created_by=world["admin_id"],
                         filename="b.dump", file_path="/backups/b.dump",
                         file_size_bytes=1, checksum_sha256="b", status=BackupStatus.COMPLETED,
                         verified=True, error_message=None,
                         completed_at=datetime.now(timezone.utc))

    items = repo.list_all()
    ids = [item.id for item in items]
    assert ids.index(second.id) < ids.index(first.id)


def test_update_verification_only_touches_verified_fields(world):
    repo = _repo()
    created = repo.record(organization_id=world["org_id"], created_by=world["admin_id"],
                          filename="c.dump", file_path="/backups/c.dump",
                          file_size_bytes=1, checksum_sha256="c", status=BackupStatus.COMPLETED,
                          verified=False, error_message=None,
                          completed_at=datetime.now(timezone.utc))
    assert created.verified is False
    assert created.verified_at is None

    updated = repo.update_verification(created.id, verified=True)
    assert updated.verified is True
    assert updated.verified_at is not None
    assert updated.filename == "c.dump"   # untouched


def test_update_verification_missing_backup_returns_none(world):
    assert _repo().update_verification(uuid.uuid4(), verified=True) is None
