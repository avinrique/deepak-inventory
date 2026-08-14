"""add username and phone to users; rename permission codes; split
users.manage into granular users.* permissions

Revision ID: c08912894e62
Revises: 222422423b7c
Create Date: 2026-08-15 00:31:07.163614

Three independent changes, bundled in one migration because they were all
part of the same User Management System pass:

1. users.username / users.phone — new columns. username is backfilled from
   the existing email's local-part (deduplicated by appending a slice of
   the user's own id where two local-parts collide) before being made
   NOT NULL + unique, since autogenerate's straight `nullable=False` would
   fail outright against a users table that already has rows (same
   situation as 205f1c3347a5_add_must_change_password_to_users.py).

2. Renames a handful of `permissions.code` values in place (UPDATE, not
   delete+insert) so every existing `role_permissions` grant pointing at
   that permission's id keeps working with zero data loss — an install
   that already seeded the old singular product.read/purchase.read/etc.
   codes keeps its role grants after this migration, just under the new
   code strings. See app.security.permissions.RENAMED_PERMISSION_CODES,
   which this migration reads from (single source of truth for the
   mapping, not duplicated here).

3. Splits the old "users.manage" umbrella into six granular users.* codes
   (view/create/update/deactivate/reset_password/manage_roles — see
   app.security.permissions.RETIRED_USERS_MANAGE_REPLACEMENT). Every role
   that was granted users.manage is granted all six replacements instead,
   then the users.manage permission row itself is deleted (which cascades
   to remove its now-empty role_permissions rows via
   RolePermission.permission_id's ON DELETE CASCADE).

Downgrade reverses the schema changes exactly. The permission-catalog
reversal is necessarily best-effort: it renames codes back and re-inserts
"users.manage", but a role that was granted only e.g. users.view (not the
full six) after the upgrade will downgrade to holding users.manage anyway,
since the six granular grants collapse back into the one umbrella code
they were expanded from — full byte-for-byte reversal of a data
migration's fan-out isn't reconstructable and isn't the goal here; a clean
schema/catalog revert is.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c08912894e62'
down_revision: Union[str, None] = '222422423b7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in sync with app.security.permissions by hand (a migration must not
# import application code — the app's catalog can keep changing after this
# migration ships, but this file has to keep describing exactly what it
# did at the time it ran).
_RENAMED_PERMISSION_CODES = {
    "product.create": "products.create",
    "product.read": "products.view",
    "product.update": "products.update",
    "product.delete": "products.delete",
    "inventory.read": "inventory.view",
    "sales.read": "sales.view",
    "purchase.create": "purchases.create",
    "purchase.read": "purchases.view",
    "purchase.update": "purchases.update",
    "purchase.approve": "purchases.approve",
    "purchase.receive": "purchases.receive",
    "purchase.cancel": "purchases.cancel",
    "purchase.return": "purchases.return",
}

_NEW_PERMISSIONS = [
    ("users.view", "View staff accounts and roles"),
    ("users.create", "Create staff accounts"),
    ("users.update", "Update/reactivate staff accounts"),
    ("users.deactivate", "Deactivate staff accounts"),
    ("users.reset_password", "Reset a staff account's password"),
    ("users.manage_roles", "Assign roles to staff accounts"),
    ("reports.export", "Export/print reports (CSV/Excel/PDF)"),
    ("settings.view", "View organization settings"),
]

_USERS_MANAGE_REPLACEMENT = [
    "users.view", "users.create", "users.update", "users.deactivate",
    "users.reset_password", "users.manage_roles",
]


def upgrade() -> None:
    # -- 1. users.username / users.phone --------------------------------#
    op.add_column('users', sa.Column('username', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('phone', sa.String(length=32), nullable=True))

    # Backfill: lower-cased email local-part, sanitized to the same
    # character set the model's check constraint requires app-side
    # (app.services.user_service._derive_username mirrors this), with the
    # first 8 hex chars of the user's own id appended on collision — id is
    # already unique, so this guarantees username uniqueness without a
    # second pass.
    op.execute("""
        WITH candidates AS (
            SELECT id,
                   regexp_replace(lower(split_part(email, '@', 1)), '[^a-z0-9._-]+', '-', 'g')
                   AS base
            FROM users
        ),
        numbered AS (
            SELECT id, base,
                   row_number() OVER (PARTITION BY base ORDER BY id) AS rn
            FROM candidates
        )
        UPDATE users
        SET username = CASE WHEN numbered.rn = 1 THEN numbered.base
                            ELSE numbered.base || '-' || substr(numbered.id::text, 1, 8) END
        FROM numbered
        WHERE users.id = numbered.id
    """)

    op.alter_column('users', 'username', nullable=False)
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_check_constraint('ck_users_username_lowercase', 'users',
                               'username = lower(username)')
    op.create_check_constraint('ck_users_username_not_blank', 'users',
                               'length(trim(username)) > 0')

    # -- 2. rename existing permission codes in place --------------------#
    conn = op.get_bind()
    for old_code, new_code in _RENAMED_PERMISSION_CODES.items():
        conn.execute(sa.text("UPDATE permissions SET code = :new WHERE code = :old"),
                    {"new": new_code, "old": old_code})

    # -- 3. new granular permissions (idempotent insert) ------------------#
    for code, description in _NEW_PERMISSIONS:
        conn.execute(sa.text("""
            INSERT INTO permissions (id, code, description, created_at, updated_at)
            VALUES (gen_random_uuid(), :code, :description, now(), now())
            ON CONFLICT (code) DO NOTHING
        """), {"code": code, "description": description})

    # -- 4. split users.manage -> the six granular codes -------------------#
    users_manage_id = conn.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'users.manage'")).scalar()
    if users_manage_id is not None:
        role_ids = [row[0] for row in conn.execute(sa.text(
            "SELECT role_id FROM role_permissions WHERE permission_id = :pid"),
            {"pid": users_manage_id})]
        for role_id in role_ids:
            for new_code in _USERS_MANAGE_REPLACEMENT:
                new_perm_id = conn.execute(
                    sa.text("SELECT id FROM permissions WHERE code = :code"),
                    {"code": new_code}).scalar()
                conn.execute(sa.text("""
                    INSERT INTO role_permissions (role_id, permission_id, granted_at)
                    VALUES (:role_id, :permission_id, now())
                    ON CONFLICT (role_id, permission_id) DO NOTHING
                """), {"role_id": role_id, "permission_id": new_perm_id})
        # Cascades to delete the now-retired role_permissions rows too
        # (RolePermission.permission_id is ON DELETE CASCADE).
        conn.execute(sa.text("DELETE FROM permissions WHERE id = :pid"),
                    {"pid": users_manage_id})


def downgrade() -> None:
    conn = op.get_bind()

    # -- reverse 4/3: collapse the six granular codes back to users.manage -#
    conn.execute(sa.text("""
        INSERT INTO permissions (id, code, description, created_at, updated_at)
        VALUES (gen_random_uuid(), 'users.manage',
               'Create/activate/deactivate users, reset passwords, assign roles',
               now(), now())
        ON CONFLICT (code) DO NOTHING
    """))
    users_manage_id = conn.execute(
        sa.text("SELECT id FROM permissions WHERE code = 'users.manage'")).scalar()

    granular_ids = [row[0] for row in conn.execute(sa.text(
        "SELECT id FROM permissions WHERE code = ANY(:codes)"),
        {"codes": _USERS_MANAGE_REPLACEMENT})]
    if granular_ids:
        role_ids = [row[0] for row in conn.execute(sa.text(
            "SELECT DISTINCT role_id FROM role_permissions WHERE permission_id = ANY(:pids)"),
            {"pids": granular_ids})]
        for role_id in role_ids:
            conn.execute(sa.text("""
                INSERT INTO role_permissions (role_id, permission_id, granted_at)
                VALUES (:role_id, :permission_id, now())
                ON CONFLICT (role_id, permission_id) DO NOTHING
            """), {"role_id": role_id, "permission_id": users_manage_id})

    for code, _description in _NEW_PERMISSIONS:
        conn.execute(sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code})

    # -- reverse 2: rename codes back ------------------------------------#
    for old_code, new_code in _RENAMED_PERMISSION_CODES.items():
        conn.execute(sa.text("UPDATE permissions SET code = :old WHERE code = :new"),
                    {"old": old_code, "new": new_code})

    # -- reverse 1: users.username / users.phone --------------------------#
    op.drop_constraint('ck_users_username_not_blank', 'users', type_='check')
    op.drop_constraint('ck_users_username_lowercase', 'users', type_='check')
    op.drop_index('ix_users_username', table_name='users')
    op.drop_column('users', 'phone')
    op.drop_column('users', 'username')
