"""AuditLog — append-only record of who did what. Rows are never updated or
deleted by the application (no UpdatedAt/soft-delete here on purpose); only
INSERT is a legitimate operation against this table.

organization_id/user_id are nullable with ON DELETE SET NULL rather than
CASCADE: an audit trail should outlive the org/user it refers to. Because
the FK can go stale that way, actor_email/organization_name are captured as
a point-in-time text snapshot — the one deliberate denormalization in this
schema, and it's justified precisely because the normalized reference
(the FK) is allowed to become null.
"""
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class AuditLog(UUIDPKMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("length(trim(action)) > 0", name="ck_audit_logs_action_not_blank"),
        CheckConstraint(
            "entity_id IS NULL OR entity_type IS NOT NULL",
            name="ck_audit_logs_entity_type_required_with_id"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_logs_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_user_id", "user_id"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Point-in-time snapshot, kept even if the FKs above go null — see the
    # module docstring for why this is the deliberate exception to
    # "avoid unnecessary denormalization".
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    organization_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # e.g. "bill.create", "user.login", "role.permission_granted"
    action: Mapped[str] = mapped_column(String(150), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Arbitrary before/after snapshots for change tracking; shape is
    # action-specific by design, not modeled as columns.
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AuditLog(id={self.id!r}, action={self.action!r})"
