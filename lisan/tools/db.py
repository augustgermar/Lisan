"""One way to open the shared SQLite database.

Every component that touches the index — the scheduler thread, capture-time
drains, CLI workers, ingestion — goes through this connect() so they all get
the same row factory, the same busy timeout, and WAL journaling. Without the
timeout, concurrent BEGIN IMMEDIATE claims raise "database is locked" instead
of briefly waiting their turn. Without WAL, any writer blocks every reader
(rollback-journal semantics) — with several processes sharing this file
(telegram service, hourly jobs worker, CLI invocations, codex children), that
is a standing invitation to lock storms; it crashed the jobs service on
2026-07-05 and fed a false "database lock" diagnosis on 2026-07-06.

WAL is a property of the database file: the first connect converts it, and
every later connection inherits it regardless of who opened it. The pragmas
are best-effort — a read-only or locked moment must not turn opening the
database into a crash.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..paths import sqlite_path

BUSY_TIMEOUT_MS = 5000


def connect(db_path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    target = db_path or sqlite_path()
    if readonly:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not readonly:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn
