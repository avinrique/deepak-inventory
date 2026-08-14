#!/usr/bin/env python3
"""Database initialization: runs Alembic migrations to head, then seeds the
baseline Role/Permission catalog from app.security.permissions (the single
source of truth — edit that module, not this script, to change the
catalog). Idempotent — safe to re-run against an already-initialized
database.

    cd inventory_system
    python scripts/init_db.py
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # so alembic.ini's relative script_location resolves

from alembic import command
from alembic.config import Config

from app.config.settings import settings
from app.database.session import get_session
from app.models import Permission, Role, RolePermission
from app.security.permissions import PERMISSIONS, ROLE_PERMISSIONS


def run_migrations() -> None:
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


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


def main() -> None:
    print(f"Running migrations against {settings.database_url} ...")
    run_migrations()

    print("Seeding baseline roles/permissions ...")
    with get_session() as session:
        permissions_by_code = seed_permissions(session)
        seed_roles(session, permissions_by_code)

    print("Database initialized.")


if __name__ == "__main__":
    main()
