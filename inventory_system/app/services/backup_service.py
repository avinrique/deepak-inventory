"""Application service for database backup/restore — the one seam the UI
is allowed to call. app.backup.postgres_backup does the actual pg_dump/
pg_restore subprocess work and never touches application state; this
layer adds permission enforcement, persists a BackupRepository row for
every attempt (success or failure, so history shows failures too), and
writes an audit log entry for every create/restore/verify, regardless of
outcome — an attempted-but-failed restore is exactly the kind of thing an
audit trail needs to show.

Both backup.create and backup.restore are OWNER/ADMIN-only permissions
(see app.security.permissions) — there is no narrower role for this on
purpose: a bad restore replaces the whole database.

Backup location and retention are Settings values, not hardcoded:
Organization.backup_directory overrides the installation's
INVENTORY_BACKUP_DIR default (see app.config.settings) when set, and
Organization.backup_retention_count (if set) prunes the oldest completed
backups — file and history row together — after each successful create.
run_scheduled_backup_if_due implements Organization.backup_frequency: a
*preference* checked once at app startup (see MainWindow), not an OS-level
cron — this is a desktop app with no background service, so "automatic"
only means "due, and the app happened to be opened."

A restore rewinds the *entire* database, including this app's own
database_backups and audit_logs tables — a backup or audit entry created
after the point a backup was taken will not survive restoring that
backup, the same way it wouldn't survive restoring any other table. Only
the "backup.restore" audit entry written immediately after pg_restore
completes is guaranteed to exist afterward, since it's inserted into the
already-restored database.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.backup import postgres_backup
from app.config.settings import settings
from app.core.exceptions import (
    BackupFailedError,
    BackupNotFoundError,
    BackupNotVerifiedError,
    BackupRestoreFailedError,
)
from app.domain.backup import BackupStatus, is_backup_due
from app.repositories.interfaces import AuditLogRepository, BackupRepository, OrganizationRepository
from app.schemas.backup import BackupOut
from app.schemas.organization import OrganizationOut
from app.security.authorization import require_permission
from app.security.session import SessionManager


class BackupService:
    def __init__(self, backups: BackupRepository, sessions: SessionManager,
                audit_log: AuditLogRepository, organizations: OrganizationRepository):
        self._backups = backups
        self._sessions = sessions
        self._audit_log = audit_log
        self._organizations = organizations

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _current_user_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).user_id

    def _audit(self, *, action: str, entity_id: uuid.UUID | None,
              changes: dict | None) -> None:
        session = self._sessions.peek()
        self._audit_log.record(
            organization_id=session.organization_id if session else None,
            user_id=session.user_id if session else None, actor_email=None,
            organization_name=None, action=action, entity_type="database_backup",
            entity_id=entity_id, changes=changes)

    @require_permission("backup.create")
    def create_backup(self) -> BackupOut:
        org_id = self._organization_id()
        user_id = self._current_user_id()
        org = self._organizations.get_by_id(org_id)
        backup_dir = (org.backup_directory if org and org.backup_directory
                     else settings.backup_dir)

        outcome = postgres_backup.create_backup(settings.database_url, backup_dir)

        record = self._backups.record(
            organization_id=org_id, created_by=user_id, filename=outcome.filename,
            file_path=outcome.file_path, file_size_bytes=outcome.file_size_bytes,
            checksum_sha256=outcome.checksum_sha256,
            status=BackupStatus.COMPLETED if outcome.success else BackupStatus.FAILED,
            verified=outcome.verified, error_message=outcome.error_message,
            completed_at=datetime.now(timezone.utc))

        self._audit(action="backup.create", entity_id=record.id,
                   changes={"filename": outcome.filename, "success": outcome.success,
                           "verified": outcome.verified,
                           "error": outcome.error_message})

        if not outcome.success:
            raise BackupFailedError(outcome.error_message)

        if org is not None:
            self._prune_old_backups(org)
        return record

    def _prune_old_backups(self, org: OrganizationOut) -> None:
        if not org.backup_retention_count:   # None or 0 -> keep everything
            return
        completed = [b for b in self._backups.list_all() if b.status == BackupStatus.COMPLETED]
        # list_all() is newest-first — everything past the retention count
        # is the oldest excess.
        for stale in completed[org.backup_retention_count:]:
            Path(stale.file_path).unlink(missing_ok=True)
            self._backups.delete(stale.id)

    def run_scheduled_backup_if_due(self) -> BackupOut | None:
        """Best-effort — called once at app startup (see MainWindow), never
        from a UI action. Silently does nothing (rather than raising) if
        the current session can't create backups, if backup_frequency is
        MANUAL, or if nothing is due yet: this isn't a user-initiated
        request, so there's no one to show a permission or validation
        error to.
        """
        session = self._sessions.peek()
        if session is None:
            return None
        if not (session.is_superuser or "backup.create" in session.permissions):
            return None
        if session.organization_id is None:
            return None

        org = self._organizations.get_by_id(session.organization_id)
        if org is None:
            return None

        completed = [b for b in self._backups.list_all() if b.status == BackupStatus.COMPLETED]
        last_completed_at = completed[0].completed_at if completed else None
        if not is_backup_due(org.backup_frequency, last_completed_at,
                             datetime.now(timezone.utc)):
            return None
        return self.create_backup()

    @require_permission("backup.create")
    def list_backups(self) -> list[BackupOut]:
        return self._backups.list_all()

    @require_permission("backup.create")
    def get_backup(self, backup_id: uuid.UUID) -> BackupOut:
        backup = self._backups.get_by_id(backup_id)
        if backup is None:
            raise BackupNotFoundError(backup_id)
        return backup

    @require_permission("backup.create")
    def verify_backup(self, backup_id: uuid.UUID) -> BackupOut:
        backup = self._backups.get_by_id(backup_id)
        if backup is None:
            raise BackupNotFoundError(backup_id)

        verified = postgres_backup.verify_backup_file(backup.file_path)
        updated = self._backups.update_verification(backup_id, verified=verified)
        if updated is None:
            raise BackupNotFoundError(backup_id)

        self._audit(action="backup.verify", entity_id=backup_id,
                   changes={"filename": backup.filename, "verified": verified})
        return updated

    @require_permission("backup.restore")
    def restore_backup(self, backup_id: uuid.UUID) -> None:
        backup = self._backups.get_by_id(backup_id)
        if backup is None:
            raise BackupNotFoundError(backup_id)
        if not backup.verified:
            raise BackupNotVerifiedError(backup_id)

        outcome = postgres_backup.restore_backup(settings.database_url, backup.file_path)

        self._audit(action="backup.restore", entity_id=backup_id,
                   changes={"filename": backup.filename, "success": outcome.success,
                           "error": outcome.error_message})

        if not outcome.success:
            raise BackupRestoreFailedError(outcome.error_message)
