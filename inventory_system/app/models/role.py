"""Role/Permission catalog. Roles are global (a catalog every organization
picks from), assigned to a user *within* a given organization via
UserOrganization — see organization.py. Keeping the catalog global (rather
than duplicated per-organization) is the deliberate normalization choice
here: "Admin" means the same set of permissions everywhere.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Role(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "roles"
    __table_args__ = (
        Index("ix_roles_name", "name", unique=True),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Built-in roles (e.g. "Owner") — protects against deletion/rename from
    # the UI later; enforced in the service layer, not the database.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Role(id={self.id!r}, name={self.name!r})"


class Permission(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint("code = lower(code)", name="ck_permissions_code_lowercase"),
        Index("ix_permissions_code", "code", unique=True),
    )

    # Dotted convention, e.g. "bills.create", "stock.adjust" — enforced
    # lower-case by the check constraint above.
    code: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Permission(id={self.id!r}, code={self.code!r})"


class RolePermission(Base):
    """Role <-> Permission grant. A pure join with a natural composite key
    (no surrogate UUID id — the pair *is* the identity, and a separate id
    would just be redundant denormalization for a row with no other
    identity of its own).
    """
    __tablename__ = "role_permissions"
    __table_args__ = (
        Index("ix_role_permissions_permission_id", "permission_id"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
    # Who granted it — audit-lite. Nullable + SET NULL: the grant itself
    # should outlive the granting user's account.
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    role: Mapped["Role"] = relationship(back_populates="permissions")
    permission: Mapped["Permission"] = relationship()
