#!/usr/bin/env python3
"""Database initialization from the command line.

Runs the Alembic migrations to head, seeds the baseline Role/Permission
catalog, and can create the first Organization + OWNER account. Idempotent —
safe to re-run against an already-initialized database.

The actual work lives in app.database.bootstrap, which the first-run setup
wizard also calls, so an administrator running this and a shop owner running
the installer get identical results.

    cd inventory_system
    python scripts/init_db.py                  # schema + role catalog
    python scripts/init_db.py --create-owner    # ...and the first account

Most installations never need this: the application offers to do all of it
on first launch.
"""
import argparse
import getpass
import sys
from pathlib import Path

# Running as a script, not a module, so the project directory has to be
# importable. Deliberately no os.chdir(): app.database.bootstrap resolves
# alembic.ini and migrations/ absolutely, via app.core.paths.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import store  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.core.logging_config import configure_logging  # noqa: E402
from app.database import bootstrap  # noqa: E402


def _prompt_owner() -> dict:
    print("\nCreate the first administrator account.")
    details = {
        "organization_name": input("  Business name: ").strip(),
        "full_name": input("  Your full name: ").strip(),
        "email": input("  Email: ").strip(),
    }
    password = getpass.getpass("  Password: ")
    if password != getpass.getpass("  Confirm password: "):
        raise SystemExit("Passwords did not match.")
    details["password"] = password
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--create-owner", action="store_true",
                        help="Also create the first organization and OWNER account.")
    args = parser.parse_args()

    configure_logging()
    if not settings.database_url:
        print("No database is configured. Set INVENTORY_DATABASE_URL, or run the "
              "application and use the setup wizard.", file=sys.stderr)
        return 1

    # Redacted: this used to print the full URL, password included, which
    # then landed in shell history, CI logs and screenshots.
    print(f"Target database: {store.redacted_url(settings.database_url)}")

    print("Running migrations ...")
    bootstrap.run_migrations(settings.database_url)

    print("Seeding baseline roles/permissions ...")
    bootstrap.seed_catalog()

    if args.create_owner:
        if bootstrap.has_any_users():
            print("This database already has user accounts — skipping owner creation.")
        else:
            bootstrap.create_first_owner(**_prompt_owner())
            print("Administrator account created.")

    print("Database initialized.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
