"""SQLAlchemy 2.x declarative base + common mixins shared by every model.

Requires PostgreSQL 13+ (for the in-core ``gen_random_uuid()``... note we
don't actually rely on that: UUIDs are generated client-side by Python's
``uuid.uuid4()`` via ``default=``, not server-side, so a row's id is known
immediately after construction, before flush/commit, and there is no
Postgres-version dependency for id generation at all.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDPKMixin:
    """UUID primary key, generated client-side (see module docstring)."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)


class TimestampMixin:
    """Timezone-aware created_at/updated_at, maintained by the database.

    ``updated_at``'s ``onupdate`` fires for ORM-driven UPDATEs. A raw SQL
    UPDATE that bypasses the ORM would not trigger it — acceptable for this
    application (all writes go through SQLAlchemy), called out here so it
    isn't a silent surprise if that assumption ever changes.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now(),
                                                  onupdate=func.now())
