"""SQLAlchemy-backed UserRepository — the real, only implementation (see
UserRepository's docstring for why there's no excel/ counterpart).

Each method opens its own transaction via app.database.session.get_session()
and converts ORM rows to Pydantic schemas before returning, so ORM instances
never leak past this module — Services only ever see app.schemas types.
"""
import uuid
from datetime import datetime

from app.database.session import get_session
from app.models import Role, User, UserOrganization
from app.schemas.user import MembershipOut, UserCredentials, UserOut


def _to_user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, full_name=user.full_name,
                   is_active=user.is_active, is_superuser=user.is_superuser,
                   must_change_password=user.must_change_password,
                   created_at=user.created_at, last_login_at=user.last_login_at)


def _to_credentials(user: User) -> UserCredentials:
    return UserCredentials(id=user.id, email=user.email,
                           hashed_password=user.hashed_password, is_active=user.is_active,
                           is_superuser=user.is_superuser,
                           must_change_password=user.must_change_password)


def _to_membership_out(m: UserOrganization, role_name: str) -> MembershipOut:
    return MembershipOut(user_id=m.user_id, organization_id=m.organization_id,
                         role_id=m.role_id, role_name=role_name, is_default=m.is_default)


class SqlUserRepository:
    def get_by_id(self, user_id: uuid.UUID) -> UserOut | None:
        with get_session() as db:
            user = db.get(User, user_id)
            return _to_user_out(user) if user else None

    def get_credentials_by_email(self, email: str) -> UserCredentials | None:
        with get_session() as db:
            user = db.query(User).filter_by(email=email.strip().lower()).one_or_none()
            return _to_credentials(user) if user else None

    def get_credentials_by_id(self, user_id: uuid.UUID) -> UserCredentials | None:
        with get_session() as db:
            user = db.get(User, user_id)
            return _to_credentials(user) if user else None

    def get_membership(self, user_id: uuid.UUID,
                       organization_id: uuid.UUID) -> MembershipOut | None:
        with get_session() as db:
            m = db.get(UserOrganization, (user_id, organization_id))
            if m is None:
                return None
            return _to_membership_out(m, m.role.name)

    def list_memberships(self, user_id: uuid.UUID) -> list[MembershipOut]:
        with get_session() as db:
            rows = db.query(UserOrganization).filter_by(user_id=user_id).all()
            return [_to_membership_out(m, m.role.name) for m in rows]

    def get_role_permissions(self, role_id: uuid.UUID) -> frozenset[str]:
        with get_session() as db:
            role = db.get(Role, role_id)
            if role is None:
                return frozenset()
            return frozenset(rp.permission.code for rp in role.permissions)

    def create_user(self, email: str, full_name: str, hashed_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID,
                    is_default: bool = True) -> UserOut:
        with get_session() as db:
            user = User(email=email.strip().lower(), full_name=full_name,
                       hashed_password=hashed_password)
            db.add(user)
            db.flush()  # user.id is populated client-side already, but this
                        # also surfaces a duplicate-email IntegrityError here
                        # rather than on some later, harder-to-attribute flush
            db.add(UserOrganization(user_id=user.id, organization_id=organization_id,
                                    role_id=role_id, is_default=is_default))
            db.flush()
            return _to_user_out(user)

    def set_active(self, user_id: uuid.UUID, is_active: bool) -> None:
        with get_session() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.is_active = is_active

    def update_password_hash(self, user_id: uuid.UUID, new_hash: str,
                             must_change_password: bool = False) -> None:
        with get_session() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.hashed_password = new_hash
                user.must_change_password = must_change_password

    def clear_must_change_password(self, user_id: uuid.UUID) -> None:
        with get_session() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.must_change_password = False

    def record_login(self, user_id: uuid.UUID, when: datetime) -> None:
        with get_session() as db:
            user = db.get(User, user_id)
            if user is not None:
                user.last_login_at = when
