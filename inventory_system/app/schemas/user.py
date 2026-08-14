import uuid
from datetime import datetime

from pydantic import BaseModel


class UserOut(BaseModel):
    """Public-ish user view — never includes hashed_password."""
    id: uuid.UUID
    email: str
    username: str
    full_name: str
    phone: str | None
    is_active: bool
    is_superuser: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None


class UserCredentials(BaseModel):
    """Internal, auth-only: the one place hashed_password crosses the
    repository boundary, used solely by AuthService to verify a login.
    """
    id: uuid.UUID
    email: str
    hashed_password: str
    is_active: bool
    is_superuser: bool
    must_change_password: bool


class MembershipOut(BaseModel):
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role_id: uuid.UUID
    role_name: str
    is_default: bool


class UserSummaryOut(BaseModel):
    """One row of UserRepository.list_users — a user plus their role within
    the organization being listed (the same join UserService.list_users
    scopes by session.organization_id), for the Users admin screen.
    """
    id: uuid.UUID
    email: str
    username: str
    full_name: str
    phone: str | None
    is_active: bool
    is_superuser: bool
    must_change_password: bool
    role_id: uuid.UUID
    role_name: str
    created_at: datetime
    last_login_at: datetime | None


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_system: bool


class UserCreate(BaseModel):
    email: str
    full_name: str
    initial_password: str
    organization_id: uuid.UUID
    role_id: uuid.UUID
    username: str | None = None
    phone: str | None = None
