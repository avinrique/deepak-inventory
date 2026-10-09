"""Assembling a PostgreSQL connection URL from its parts.

Lives here rather than in app.ui.setup_wizard, where it started, because
app.database.local_server also has to build one and the dependency only runs
one way: ui imports from database, never the reverse.
"""
from sqlalchemy.engine import URL

DRIVER = "postgresql+psycopg"


def build_url(*, host: str, port: int, database: str, username: str,
              password: str, sslmode: str) -> str:
    """Assembles a DSN from what the user typed.

    URL.create() percent-encodes each component, which is the entire reason
    this is not an f-string: a password containing '@', ':' or a space is
    perfectly legal and produces a URL that silently parses into the wrong
    username and host if it is pasted together by hand.
    """
    return URL.create(
        drivername=DRIVER, username=username or None, password=password or None,
        host=host or None, port=port or None, database=database or None,
        query={"sslmode": sslmode} if sslmode else {},
    ).render_as_string(hide_password=False)
