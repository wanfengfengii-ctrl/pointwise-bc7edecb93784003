from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager


def connect(path: str) -> sqlite3.Connection:
    """Open one short-lived connection to a single SQLite store."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit write transaction; commits once, rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass
