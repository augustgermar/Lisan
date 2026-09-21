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
import time
from pathlib import Path
from typing import Callable, TypeVar

from ..paths import sqlite_path

T = TypeVar("T")

# Background maintenance and large ingestion/index transactions can legitimately
# exceed five seconds. Waiting here is safer than turning temporary contention
# into a terminal job failure; WAL still permits readers during the wait.
BUSY_TIMEOUT_MS = 30000


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


def retry_locked(fn: Callable[[], T], *, attempts: int = 4, base_delay: float = 0.5) -> T:
    """Run ``fn`` (a full connect-write-commit unit), retrying on "database
    is locked". busy_timeout already absorbs brief contention inside one
    connection; this is for the caller who lands mid-way through a long
    writer (a full reindex — see rebuild_index's REINDEX_CHUNK) and would
    otherwise drop real work — a queued capture job, an ingested file — on
    a single unlucky attempt instead of the transient conflict it is."""
    delay = base_delay
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
