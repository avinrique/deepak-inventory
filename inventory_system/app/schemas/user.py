import uuid
from datetime import datetime

from pydantic import BaseModel


class UserOut(BaseModel):
    """Public-ish user view — never includes hashed_password."""
    id: uuid.UUID
    email: str
    full_name: str
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


class UserCreate(BaseModel):
    email: str
    full_name: str
    initial_password: str
    organization_id: uuid.UUID
    role_id: uuid.UUID
