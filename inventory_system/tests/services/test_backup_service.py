"""BackupService tested against a hand-written fake BackupRepository and a
monkeypatched app.backup.postgres_backup — no database, no subprocess.
Proves permission enforcement (backup.create/backup.restore are separate
gates), that every attempt (success or failure) is recorded and audited,
and that restore refuses an unverified backup before ever touching
postgres_backup.restore_backup.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.services.backup_service as backup_service_module
from app.backup.postgres_backup import BackupOutcome, RestoreOutcome
from app.core.exceptions import (
    BackupFailedError,
    BackupNotFoundError,
    BackupNotVerifiedError,
    BackupRestoreFailedError,
)
from app.domain.backup import BackupStatus
from app.schemas.backup import BackupOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.backup_service import BackupService

ORG_ID = uuid.uuid4()
BACKUP_ID = uuid.uuid4()


class FakeBackupRepository:
    def __init__(self):
        self.backups: dict[uuid.UUID, BackupOut] = {}
        self.record_calls: list[dict] = []

    def record(self, *, organization_id, created_by, filename, file_path, file_size_bytes,
              checksum_sha256, status, verified, error_message, completed_at) -> BackupOut:
        self.record_calls.append({"filename": filename, "status": status})
        backup = BackupOut(id=uuid.uuid4(), created_at=datetime.now(timezone.utc),
                           completed_at=completed_at, organization_id=organization_id,
                           created_by=created_by, filename=filename, file_path=file_path,
                           file_size_bytes=file_size_bytes, checksum_sha256=checksum_sha256,
                           status=status, verified=verified, verified_at=None,
                           error_message=error_message)
        self.backups[backup.id] = backup
        return backup

    def update_verification(self, backup_id, *, verified):
        existing = self.backups.get(backup_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={"verified": verified,
                                              "verified_at": datetime.now(timezone.utc)})
        self.backups[backup_id] = updated
        return updated

    def get_by_id(self, backup_id):
        return self.backups.get(backup_id)

    def list_all(self):
        return list(self.backups.values())

    def delete(self, backup_id):
        self.backups.pop(backup_id, None)


class FakeOrganizationRepository:
    """Empty by default — BackupService treats a missing organization as
    "use the installation's default backup_dir, nothing to prune", a
    no-op unless a test explicitly seeds one via .orgs[ORG_ID] = ....
    """
    def __init__(self):
        self.orgs: dict[uuid.UUID, object] = {}

    def get_by_id(self, organization_id):
        return self.orgs.get(organization_id)

    def update(self, organization_id, data):
        raise NotImplementedError

    def get_logo(self, organization_id):
        raise NotImplementedError


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


def _service(permissions=frozenset({"backup.create", "backup.restore"}), repo=None):
    repo = repo or FakeBackupRepository()
    audit_log = FakeAuditLogRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return BackupService(repo, sessions, audit_log, FakeOrganizationRepository()), repo, audit_log


def _success_outcome(filename="inventory_20260101_000000.dump") -> BackupOutcome:
    return BackupOutcome(True, f"/backups/{filename}", filename, 100, "checksum", True, None)


def _failure_outcome(message="pg_dump: connection refused") -> BackupOutcome:
    return BackupOutcome(False, "/backups/x.dump", "x.dump", None, None, False, message)


# -- create_backup ---------------------------------------------------------#

def test_create_backup_requires_permission():
    service, repo, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_backup()
    assert repo.record_calls == []


def test_create_backup_success_records_and_audits(monkeypatch):
    service, repo, audit_log = _service()
    monkeypatch.setattr(backup_service_module.postgres_backup, "create_backup",
                        lambda *_a, **_kw: _success_outcome())

    result = service.create_backup()

    assert result.status == BackupStatus.COMPLETED
    assert repo.record_calls[0]["status"] == BackupStatus.COMPLETED
    assert audit_log.entries[-1]["action"] == "backup.create"
    assert audit_log.entries[-1]["changes"]["success"] is True


def test_create_backup_failure_still_records_and_audits_then_raises(monkeypatch):
    service, repo, audit_log = _service()
    monkeypatch.setattr(backup_service_module.postgres_backup, "create_backup",
                        lambda *_a, **_kw: _failure_outcome())

    with pytest.raises(BackupFailedError):
        service.create_backup()

    assert repo.record_calls[0]["status"] == BackupStatus.FAILED
    assert audit_log.entries[-1]["action"] == "backup.create"
    assert audit_log.entries[-1]["changes"]["success"] is False


# -- list_backups / get_backup ---------------------------------------------#

def test_list_backups_requires_permission():
    service, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.list_backups()


def test_get_backup_missing_raises_not_found():
    service, _, _ = _service()
    with pytest.raises(BackupNotFoundError):
        service.get_backup(uuid.uuid4())


# -- verify_backup ------------------------------------------------------ #

def test_verify_backup_updates_and_audits(monkeypatch):
    service, repo, audit_log = _service()
    monkeypatch.setattr(backup_service_module.postgres_backup, "create_backup",
                        lambda *_a, **_kw: _success_outcome())
    backup = service.create_backup()

    monkeypatch.setattr(backup_service_module.postgres_backup, "verify_backup_file",
                        lambda _path: False)
    result = service.verify_backup(backup.id)

    assert result.verified is False
    assert audit_log.entries[-1]["action"] == "backup.verify"
    assert audit_log.entries[-1]["changes"]["verified"] is False


def test_verify_backup_missing_raises_not_found():
    service, _, _ = _service()
    with pytest.raises(BackupNotFoundError):
        service.verify_backup(uuid.uuid4())


# -- restore_backup -----------------------------------------------------#

def test_restore_backup_requires_backup_restore_permission_not_just_create():
    service, repo, _ = _service(permissions=frozenset({"backup.create"}))
    with pytest.raises(PermissionDeniedError):
        service.restore_backup(uuid.uuid4())


def test_restore_backup_missing_raises_not_found():
    service, _, _ = _service()
    with pytest.raises(BackupNotFoundError):
        service.restore_backup(uuid.uuid4())


def test_restore_backup_refuses_unverified_backup_without_calling_postgres_backup(monkeypatch):
    service, repo, audit_log = _service()
    repo.backups[BACKUP_ID] = BackupOut(
        id=BACKUP_ID, created_at=datetime.now(timezone.utc), completed_at=None,
        organization_id=ORG_ID, created_by=None, filename="unverified.dump",
        file_path="/backups/unverified.dump", file_size_bytes=1, checksum_sha256="x",
        status=BackupStatus.COMPLETED, verified=False, verified_at=None, error_message=None)

    called = []
    monkeypatch.setattr(backup_service_module.postgres_backup, "restore_backup",
                        lambda *_a, **_kw: called.append(1))

    with pytest.raises(BackupNotVerifiedError):
        service.restore_backup(BACKUP_ID)
    assert called == []
    assert audit_log.entries == []  # refused before any audit-worthy attempt


def test_restore_backup_success_audits(monkeypatch):
    service, repo, audit_log = _service()
    repo.backups[BACKUP_ID] = BackupOut(
        id=BACKUP_ID, created_at=datetime.now(timezone.utc), completed_at=None,
        organization_id=ORG_ID, created_by=None, filename="verified.dump",
        file_path="/backups/verified.dump", file_size_bytes=1, checksum_sha256="x",
        status=BackupStatus.COMPLETED, verified=True, verified_at=datetime.now(timezone.utc),
        error_message=None)
    monkeypatch.setattr(backup_service_module.postgres_backup, "restore_backup",
                        lambda *_a, **_kw: RestoreOutcome(True, None))

    service.restore_backup(BACKUP_ID)   # must not raise

    assert audit_log.entries[-1]["action"] == "backup.restore"
    assert audit_log.entries[-1]["changes"]["success"] is True


def test_restore_backup_failure_audits_then_raises(monkeypatch):
    service, repo, audit_log = _service()
    repo.backups[BACKUP_ID] = BackupOut(
        id=BACKUP_ID, created_at=datetime.now(timezone.utc), completed_at=None,
        organization_id=ORG_ID, created_by=None, filename="verified.dump",
        file_path="/backups/verified.dump", file_size_bytes=1, checksum_sha256="x",
        status=BackupStatus.COMPLETED, verified=True, verified_at=datetime.now(timezone.utc),
        error_message=None)
    monkeypatch.setattr(
        backup_service_module.postgres_backup, "restore_backup",
        lambda *_a, **_kw: RestoreOutcome(False, "pg_restore: connection refused"))

    with pytest.raises(BackupRestoreFailedError):
        service.restore_backup(BACKUP_ID)

    assert audit_log.entries[-1]["action"] == "backup.restore"
    assert audit_log.entries[-1]["changes"]["success"] is False
