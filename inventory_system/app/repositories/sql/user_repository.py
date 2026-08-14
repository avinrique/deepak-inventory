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
from app.schemas.user import MembershipOut, RoleOut, UserCredentials, UserOut, UserSummaryOut


def _to_user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, username=user.username,
                   full_name=user.full_name, phone=user.phone, is_active=user.is_active,
                   is_superuser=user.is_superuser,
                   must_change_password=user.must_change_password,
                   created_at=user.created_at, last_login_at=user.last_login_at)


def _to_role_out(role: Role) -> RoleOut:
    return RoleOut(id=role.id, name=role.name, description=role.description,
                   is_system=role.is_system)


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

    def list_users(self, organization_id: uuid.UUID) -> list[UserSummaryOut]:
        with get_session() as db:
            rows = (db.query(UserOrganization)
                   .filter_by(organization_id=organization_id)
                   .join(User, User.id == UserOrganization.user_id)
                   .order_by(User.full_name)
                   .all())
            return [
                UserSummaryOut(id=m.user.id, email=m.user.email, username=m.user.username,
                              full_name=m.user.full_name, phone=m.user.phone,
                              is_active=m.user.is_active, is_superuser=m.user.is_superuser,
                              must_change_password=m.user.must_change_password,
                              role_id=m.role_id, role_name=m.role.name,
                              created_at=m.user.created_at,
                              last_login_at=m.user.last_login_at)
                for m in rows
            ]

    def list_roles(self) -> list[RoleOut]:
        with get_session() as db:
            roles = db.query(Role).order_by(Role.name).all()
            return [_to_role_out(r) for r in roles]

    def update_membership_role(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                              role_id: uuid.UUID) -> MembershipOut | None:
        with get_session() as db:
            m = db.get(UserOrganization, (user_id, organization_id))
            if m is None:
                return None
            m.role_id = role_id
            db.flush()
            return _to_membership_out(m, m.role.name)

    def get_role_permissions(self, role_id: uuid.UUID) -> frozenset[str]:
        with get_session() as db:
            role = db.get(Role, role_id)
            if role is None:
                return frozenset()
            return frozenset(rp.permission.code for rp in role.permissions)

    def email_exists(self, email: str) -> bool:
        with get_session() as db:
            return db.query(User).filter_by(email=email.strip().lower()).first() is not None

    def username_exists(self, username: str) -> bool:
        with get_session() as db:
            return (db.query(User).filter_by(username=username.strip().lower()).first()
                   is not None)

    def create_user(self, email: str, full_name: str, hashed_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID, username: str,
                    phone: str | None = None, is_default: bool = True) -> UserOut:
        with get_session() as db:
            user = User(email=email.strip().lower(), username=username.strip().lower(),
                       full_name=full_name, phone=phone, hashed_password=hashed_password)
            db.add(user)
            db.flush()  # user.id is populated client-side already, but this
                        # also surfaces a duplicate-email/-username IntegrityError
                        # here rather than on some later, harder-to-attribute flush
            db.add(UserOrganization(user_id=user.id, organization_id=organization_id,
                                    role_id=role_id, is_default=is_default))
            db.flush()
            return _to_user_out(user)

    def set_active(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                   is_active: bool) -> bool:
        """Returns False (no write) if the target isn't a member of
        organization_id — the caller (UserService) is scoped to the calling
        admin's own organization, and this membership check is what stops a
        users.manage holder in one organization from reaching a user in a
        different one purely by guessing/enumerating a UUID.
        """
        with get_session() as db:
            membership = db.get(UserOrganization, (user_id, organization_id))
            if membership is None:
                return False
            user = db.get(User, user_id)
            if user is None:
                return False
            user.is_active = is_active
            return True

    def update_password_hash(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                             new_hash: str, must_change_password: bool = False) -> bool:
        """Same organization-membership guard as set_active — see its
        docstring."""
        with get_session() as db:
            membership = db.get(UserOrganization, (user_id, organization_id))
            if membership is None:
                return False
            user = db.get(User, user_id)
            if user is None:
                return False
            user.hashed_password = new_hash
            user.must_change_password = must_change_password
            return True

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
