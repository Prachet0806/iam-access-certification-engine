import contextlib
import sqlite3
from typing import Any, Iterable, Tuple

from common import config

try:
    import psycopg2  # type: ignore
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore


class Database:
    """
    Lightweight DB helper that supports SQLite and Postgres based on DB_URL.
    - SQLite: enables foreign keys pragma.
    - Postgres: connect_timeout, autocommit off by default.
    """

    def __init__(self):
        self.is_sqlite = config.db_is_sqlite()

    def _connect_sqlite(self):
        path = config.require_sqlite_path()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _connect_postgres(self):
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required for Postgres connections")
        return psycopg2.connect(config.DB_URL, connect_timeout=10)

    @contextlib.contextmanager
    def get_connection(self):
        conn = self._connect_sqlite() if self.is_sqlite else self._connect_postgres()
        try:
            yield conn
            # caller should commit; for safety commit on exit
            conn.commit()
        finally:
            conn.close()

    def prepare_sql(self, sql: str) -> str:
        """
        Convert SQLite-style ? placeholders to %s for Postgres.
        """
        if self.is_sqlite:
            return sql
        return sql.replace("?", "%s")

    def execute(self, cursor, sql: str, params: Iterable[Any] = ()):
        prepared = self.prepare_sql(sql)
        # Avoid passing empty params to drivers that expect placeholders
        if params is None or (hasattr(params, "__len__") and len(params) == 0):
            cursor.execute(prepared)
        else:
            cursor.execute(prepared, params)

    def executemany(self, cursor, sql: str, seq_of_params: Iterable[Tuple[Any, ...]]):
        cursor.executemany(self.prepare_sql(sql), seq_of_params)


db = Database()

