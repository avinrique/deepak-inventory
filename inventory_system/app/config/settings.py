"""Central settings.

Values come from environment variables (or a git-ignored .env file — see
.env.example for every key this reads) via pydantic-settings. Nothing here
is ever a real credential; defaults are safe local-dev placeholders only.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVENTORY_", env_file=".env",
                                      env_file_encoding="utf-8", extra="ignore")

    backend: str = "excel"          # "excel" | "postgres" — which backend serves
                                     # inventory (Bill/Stock/Party) data. Auth
                                     # (User/Role/...) always uses database_url,
                                     # regardless of this flag — see container.py.
    database_url: str = "postgresql+psycopg://localhost:5432/inventory"
    default_vat_percent: str = "13"
    log_dir: str = "logs"
    session_idle_timeout_minutes: int = 30

    # Deliberately separate from database_url, and unset by default: DB
    # integration tests (tests/models, tests/repositories/test_sql_*) skip
    # entirely unless this is explicitly set, so there is no way for the
    # test suite to accidentally create_all()/drop_all() against whatever
    # real, possibly-production database database_url happens to point at.
    test_database_url: str | None = None


settings = Settings()
