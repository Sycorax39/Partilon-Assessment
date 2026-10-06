"""PostgreSQL access: connection pool, startup migrations, and mapping DB outages to HTTP 503."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .errors import APIError

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str, acquire_timeout: float = 3.0):
        self.url = url
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=10,
            open=False,
            timeout=acquire_timeout,                     # max wait for a connection
            check=ConnectionPool.check_connection,       # drop dead connections (e.g. after a DB restart)
            kwargs={"row_factory": dict_row, "connect_timeout": 3},
        )

    def migrate(self, *sql_files: Path, attempts: int = 20, delay: float = 1.5) -> None:
        """Run idempotent schema + seed scripts at startup, retrying while the DB comes up."""
        for attempt in range(1, attempts + 1):
            try:
                with psycopg.connect(self.url, connect_timeout=3, autocommit=True) as conn:
                    for path in sql_files:
                        conn.execute(path.read_text())
                logger.info("database migrated", extra={"fields": {"files": [p.name for p in sql_files]}})
                return
            except psycopg.OperationalError as exc:
                logger.warning("database not ready, retrying",
                               extra={"fields": {"attempt": attempt, "error": str(exc).strip()}})
                time.sleep(delay)
        raise RuntimeError("database unavailable after retries")

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """A pooled connection (one transaction). DB outages surface as a clean 503."""
        try:
            with self.pool.connection() as conn:
                yield conn
        except (psycopg.OperationalError, PoolTimeout) as exc:
            logger.error("database unavailable", extra={"fields": {"error": str(exc).strip()}})
            raise APIError(503, "DATABASE_UNAVAILABLE",
                           "The data store is temporarily unavailable. Please retry later.") from exc

    def is_ready(self) -> bool:
        try:
            with self.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False
