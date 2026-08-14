"""User — a person who can log in. Organization membership and per-org role
live on UserOrganization, not here: a user's role is scoped to the
organization they're acting within (see organization.py), not global.
``is_superuser`` is the one deliberate exception — a platform-admin escape
hatch that bypasses org/role checks entirely.
"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Enforces the app always stores/looks up email lower-cased, so the
        # unique index below actually catches "a@b.com" vs "A@b.com".
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        Index("ix_users_email", "email", unique=True),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set on an admin-initiated password reset; cleared when the user
    # successfully changes their own password. Enforced by
    # app.security.authorization.check_permission, not just left for the UI
    # to notice.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                        default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                            nullable=True)

    memberships: Mapped[list["UserOrganization"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"User(id={self.id!r}, email={self.email!r})"
