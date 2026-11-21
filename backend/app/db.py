from contextlib import contextmanager

from .config import get_settings


@contextmanager
def get_conn():
    """One autocommit connection per use. psycopg is imported lazily so the app
    (and unit tests) import fine without a database.

    prepare_threshold=None is required for Supabase's pooled (pgbouncer,
    transaction-mode) connection string."""
    import psycopg
    from psycopg.rows import dict_row

    s = get_settings()
    if not s.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(s.database_url, row_factory=dict_row, autocommit=True, prepare_threshold=None) as conn:
        yield conn
