"""One way to open the shared SQLite database.

Every component that touches the index — the scheduler thread, capture-time
drains, CLI workers, ingestion — goes through this connect() so they all get
the same row factory and the same busy timeout. Without the timeout,
concurrent BEGIN IMMEDIATE claims raise "database is locked" instead of
briefly waiting their turn.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..paths import sqlite_path


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
