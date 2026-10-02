"""One ledger of execution runs, shared by every executor.

The Adjutant records each task attempt in `task_runs`; plan steps now do the
same, so "what did the agent try, when, and how did it end" has a single
answer instead of one per subsystem. `origin` says who owns a row — only the
Adjutant daemon may declare an `adjutant` row abandoned (it holds the one
lock, so an unfinished row there means a crash). A plan step's row belongs to
the job worker, where a step can legitimately run for as long as its own
timeout.

All writes are best-effort: the ledger is evidence, never a dependency of the
work it describes.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ORIGIN_ADJUTANT = "adjutant"
ORIGIN_PLAN = "plan"

_DDL = """
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    exit_status TEXT,
    error TEXT
)
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create task_runs if absent and backfill `origin` on older databases.
    Pre-existing rows are the Adjutant's — it was the only writer."""
    from .db import add_column_if_missing

    conn.execute(_DDL)
    add_column_if_missing(conn, "task_runs", "origin", "ALTER TABLE task_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'adjutant'")


def begin_run(db_path: Path | None, task_id: str, attempt: int, *, origin: str) -> int | None:
    """Open a run row; returns its id, or None if the ledger is unavailable."""
    from .db import connect

    try:
        conn = connect(db_path)
        try:
            ensure_table(conn)
            cursor = conn.execute(
                "INSERT INTO task_runs (task_id, attempt, started, origin) VALUES (?, ?, ?, ?)",
                (task_id, attempt, _now(), origin),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()
    except Exception:
        return None


def finish_run(
    db_path: Path | None,
    run_id: int | None,
    *,
    ok: bool,
    error: str | None = None,
    status: str | None = None,
) -> None:
    """Close a run row. `status` overrides the ok/failed label for outcomes
    that are neither (a run the owner cancelled was not a failure)."""
    if run_id is None:
        return
    from .db import connect

    try:
        conn = connect(db_path)
        try:
            conn.execute(
                "UPDATE task_runs SET finished = ?, exit_status = ?, error = ? WHERE id = ?",
                (_now(), status or ("ok" if ok else "failed"), (error or None) and error[:500], run_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
