"""DatabaseBackup — metadata for each pg_dump backup file of the whole
database. Deliberately not organization-scoped the way most other models
are: a backup covers every tenant's data in one physical Postgres
database, so there's no such thing as a per-organization backup.
organization_id/created_by are a point-in-time record of who triggered
it (nullable, ON DELETE SET NULL — same rationale as AuditLog: the
backup record should outlive the org/user that triggered it).

Never stores database credentials — see app.backup.postgres_backup's
module docstring for how the password is kept out of this table, the
subprocess argv, and any log line.
"""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.backup import BackupStatus
from app.models.base import Base, UUIDPKMixin


class DatabaseBackup(UUIDPKMixin, Base):
    __tablename__ = "database_backups"
    __table_args__ = (
        CheckConstraint("length(trim(filename)) > 0",
                        name="ck_database_backups_filename_not_blank"),
        Index("ix_database_backups_created_at", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                           nullable=True)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[BackupStatus] = mapped_column(
        Enum(BackupStatus, name="backup_status", native_enum=True), nullable=False,
        default=BackupStatus.IN_PROGRESS)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)
    # Sanitized failure detail — never contains connection strings or
    # passwords; see app.backup.postgres_backup._sanitize_error.
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DatabaseBackup(id={self.id!r}, filename={self.filename!r}, status={self.status!r})"
