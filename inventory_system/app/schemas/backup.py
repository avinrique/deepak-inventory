"""Pydantic I/O shapes for database backups — see app.models.backup for the
persisted row and app.domain.backup for the status enum they share.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.domain.backup import BackupStatus


class BackupOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    completed_at: datetime | None
    organization_id: uuid.UUID | None
    created_by: uuid.UUID | None
    filename: str
    file_path: str
    file_size_bytes: int | None
    checksum_sha256: str | None
    status: BackupStatus
    verified: bool
    verified_at: datetime | None
    error_message: str | None
