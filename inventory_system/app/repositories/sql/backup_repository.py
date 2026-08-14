"""SQLAlchemy-backed BackupRepository — persists the outcome of a backup
run (app.backup.postgres_backup does the actual pg_dump/pg_restore work
and never touches the database itself). One row per attempt, success or
failure, so backup history shows failed attempts too.
"""
import uuid
from datetime import datetime, timezone

from app.database.session import get_session
from app.domain.backup import BackupStatus
from app.models import DatabaseBackup
from app.schemas.backup import BackupOut


def _to_out(backup: DatabaseBackup) -> BackupOut:
    return BackupOut(id=backup.id, created_at=backup.created_at,
                     completed_at=backup.completed_at,
                     organization_id=backup.organization_id, created_by=backup.created_by,
                     filename=backup.filename, file_path=backup.file_path,
                     file_size_bytes=backup.file_size_bytes,
                     checksum_sha256=backup.checksum_sha256, status=backup.status,
                     verified=backup.verified, verified_at=backup.verified_at,
                     error_message=backup.error_message)


class SqlBackupRepository:
    def record(self, *, organization_id: uuid.UUID | None, created_by: uuid.UUID | None,
              filename: str, file_path: str, file_size_bytes: int | None,
              checksum_sha256: str | None, status: BackupStatus, verified: bool,
              error_message: str | None, completed_at: datetime | None) -> BackupOut:
        with get_session() as db:
            backup = DatabaseBackup(
                organization_id=organization_id, created_by=created_by, filename=filename,
                file_path=file_path, file_size_bytes=file_size_bytes,
                checksum_sha256=checksum_sha256, status=status, verified=verified,
                verified_at=datetime.now(timezone.utc) if verified else None,
                error_message=error_message, completed_at=completed_at)
            db.add(backup)
            db.flush()
            return _to_out(backup)

    def update_verification(self, backup_id: uuid.UUID, *,
                            verified: bool) -> BackupOut | None:
        with get_session() as db:
            backup = db.get(DatabaseBackup, backup_id)
            if backup is None:
                return None
            backup.verified = verified
            backup.verified_at = datetime.now(timezone.utc)
            db.flush()
            return _to_out(backup)

    def get_by_id(self, backup_id: uuid.UUID) -> BackupOut | None:
        with get_session() as db:
            backup = db.get(DatabaseBackup, backup_id)
            return _to_out(backup) if backup is not None else None

    def list_all(self) -> list[BackupOut]:
        with get_session() as db:
            rows = db.query(DatabaseBackup).order_by(DatabaseBackup.created_at.desc()).all()
            return [_to_out(row) for row in rows]

    def delete(self, backup_id: uuid.UUID) -> None:
        """Only ever called by BackupService's retention pruning, after the
        backup's file has already been removed from disk — this just drops
        the now-dangling history row.
        """
        with get_session() as db:
            backup = db.get(DatabaseBackup, backup_id)
            if backup is not None:
                db.delete(backup)
