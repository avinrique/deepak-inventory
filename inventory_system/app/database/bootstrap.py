"""Bringing a brand-new PostgreSQL database up to a usable state.

A fresh install used to be a dead end. The schema had to be created by hand
with `alembic upgrade head`, and even then nobody could log in: every path
that creates a user goes through UserService.create_user, which is
`users.create`-gated and writes an audit row from the current session — so
creating the *first* user required a session that could only exist once a
first user already did. A business user who has just run an installer
cannot be expected to break that circle from a command line.

Everything needed is here, and both callers use it — scripts/init_db.py for
an administrator at a terminal, and app.ui.setup_wizard for the person who
just ran the installer. One implementation means the wizard cannot drift out
of step with the migrations.

Every function is idempotent: running against an already-initialized
database is a no-op, not an error.
"""
import logging
import re
import secrets

from alembic import command
from alembic.config import Config

from app.core.exceptions import ResourceMissingError
from app.core.paths import resource_path
from app.database.errors import translate_db_error
from app.database.session import get_session
from app.domain.security_policy import PasswordPolicy, validate_password
from app.domain.user import normalize_email, normalize_username, validate_user
from app.models import (
    Organization,
    Permission,
    Role,
    RolePermission,
    User,
    UserOrganization,
)
from app.security.passwords import hash_password
from app.security.permissions import PERMISSIONS, ROLE_PERMISSIONS

_logger = logging.getLogger(__name__)

OWNER_ROLE = "OWNER"

# Mirrors the Organization column defaults (app/models/organization.py) --
# the organization whose policy would normally apply does not exist yet at
# the moment the first owner sets their password.
DEFAULT_PASSWORD_POLICY = PasswordPolicy(min_length=8, require_uppercase=False,
                                         require_number=False,
                                         require_special_char=False)

_USERNAME_SANITIZE_RE = re.compile(r"[^a-z0-9._-]+")


class BootstrapError(Exception):
    """The database cannot be initialized as asked. Carries a message meant
    to be shown to whoever is running the setup."""


def _alembic_config(database_url: str) -> Config:
    config_path = resource_path("alembic.ini")
    migrations_path = resource_path("migrations")
    if not config_path.is_file():
        raise ResourceMissingError(config_path)
    if not migrations_path.is_dir():
        raise ResourceMissingError(migrations_path)

    config = Config(str(config_path))
    # Both set absolutely. alembic.ini's own script_location is the relative
    # "migrations", which only resolves when the process happens to be
    # running from the project directory -- never true for an installed .exe,
    # and the reason scripts/init_db.py used to os.chdir() at import time.
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def run_migrations(database_url: str) -> None:
    """Brings the schema to head. Safe on an already-current database."""
    _logger.info("Running migrations to head")
    try:
        command.upgrade(_alembic_config(database_url), "head")
    except ResourceMissingError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a translated AppError
        raise translate_db_error(exc) from exc


def seed_permissions(session) -> dict[str, Permission]:
    by_code: dict[str, Permission] = {}
    for perm_def in PERMISSIONS:
        perm = session.query(Permission).filter_by(code=perm_def.code).one_or_none()
        if perm is None:
            perm = Permission(code=perm_def.code, description=perm_def.description)
            session.add(perm)
            session.flush()
        by_code[perm_def.code] = perm
    return by_code


def seed_roles(session, permissions_by_code: dict[str, Permission]) -> None:
    for name, codes in ROLE_PERMISSIONS.items():
        role = session.query(Role).filter_by(name=name).one_or_none()
        if role is None:
            role = Role(name=name, is_system=True)
            session.add(role)
            session.flush()
        already_granted = {rp.permission_id for rp in role.permissions}
        for code in codes:
            perm = permissions_by_code[code]
            if perm.id not in already_granted:
                session.add(RolePermission(role_id=role.id, permission_id=perm.id))


def seed_catalog() -> None:
    """Roles and permissions. Their definitions live in
    app.security.permissions -- edit that module, never this one."""
    _logger.info("Seeding role/permission catalog")
    with get_session() as session:
        seed_roles(session, seed_permissions(session))


def has_any_users() -> bool:
    with get_session() as session:
        return session.query(User.id).first() is not None


def _derive_username(session, email: str) -> str:
    """Same shape as UserService._derive_username, minus the repository.

    The users table has CHECK constraints requiring a lowercase, non-blank
    username and a unique index on it, so a sloppy value here fails as an
    IntegrityError rather than a readable message.
    """
    local_part = email.split("@", 1)[0].lower()
    base = _USERNAME_SANITIZE_RE.sub("-", local_part).strip("-") or "user"
    candidate = base
    for _ in range(5):
        if session.query(User.id).filter_by(username=candidate).first() is None:
            return candidate
        candidate = f"{base}-{secrets.token_hex(2)}"
    raise BootstrapError("Could not derive a unique username from that email address.")


def create_first_owner(*, organization_name: str, full_name: str, email: str,
                       password: str, username: str | None = None) -> None:
    """Creates the initial Organization and its OWNER, in one transaction.

    This is the only path in the application that creates a user without an
    authenticated session, which is exactly why it refuses to run against a
    database that already has one. Without that check it would be an
    unauthenticated "make me the owner" backdoor into a live system.
    """
    if not organization_name.strip():
        raise BootstrapError("Enter a name for your business.")

    email = normalize_email(email)
    explicit_username = normalize_username(username) if username else None

    errors = validate_user(full_name=full_name, email=email, username=explicit_username)
    errors += validate_password(password, DEFAULT_PASSWORD_POLICY)
    if errors:
        raise BootstrapError("\n".join(errors))

    with get_session() as session:
        if session.query(User.id).first() is not None:
            raise BootstrapError(
                "This database already has user accounts, so it does not need "
                "setting up. Log in instead, or ask an administrator to create "
                "your account.")

        owner_role = session.query(Role).filter_by(name=OWNER_ROLE).one_or_none()
        if owner_role is None:
            raise BootstrapError(
                f"The {OWNER_ROLE} role is missing from this database. "
                "Run the database setup again.")

        resolved_username = explicit_username or _derive_username(session, email)

        organization = Organization(name=organization_name.strip())
        session.add(organization)
        session.flush()

        user = User(email=email, username=resolved_username, full_name=full_name.strip(),
                    hashed_password=hash_password(password),
                    is_active=True,
                    # The first account is the platform administrator: there
                    # is nobody else who could grant it anything.
                    is_superuser=True,
                    # Nothing to force a change from -- they chose this
                    # password themselves, seconds ago.
                    must_change_password=False)
        session.add(user)
        session.flush()

        session.add(UserOrganization(user_id=user.id, organization_id=organization.id,
                                     role_id=owner_role.id, is_default=True))
        _logger.info("Created initial organization %r with owner %r",
                     organization.name, email)


def initialize(database_url: str) -> None:
    """Schema + catalog: what "Set up this database" runs before it asks for
    the owner's details."""
    run_migrations(database_url)
    seed_catalog()
