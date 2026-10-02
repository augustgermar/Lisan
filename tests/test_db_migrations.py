"""Column migrations must survive several processes starting at once.

The first time a new column shipped, a restart brought four services up together
and the adjutant daemon crashed on "database is locked" while adding it. The two
ways a plain check-then-ALTER fails under that race are exercised for real here,
with two connections and a held lock, not with mocks of mocks."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from lisan.tools.db import add_column_if_missing


@pytest.fixture()
def path(tmp_path):
    p = tmp_path / "m.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    return p


def cols(path):
    c = sqlite3.connect(path)
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(t)")]
    finally:
        c.close()


def test_adds_a_missing_column_once_and_is_idempotent(path):
    c = sqlite3.connect(path)
    assert add_column_if_missing(c, "t", "x", "ALTER TABLE t ADD COLUMN x TEXT") is True
    assert add_column_if_missing(c, "t", "x", "ALTER TABLE t ADD COLUMN x TEXT") is False
    c.commit()
    assert cols(path) == ["id", "x"]


def test_losing_the_race_to_add_the_column_is_success_not_a_crash(path):
    """Both processes see the column missing; one ALTERs first; the other must not die."""

    class Racy:
        """Reports the column missing, but the ALTER finds it already there."""

        def __init__(self, real):
            self.real = real

        def execute(self, sql, *a):
            if sql.startswith("PRAGMA"):
                return self.real.execute("PRAGMA table_info(t)") if False else _Empty()
            return self.real.execute(sql, *a)

    class _Empty:
        def fetchall(self):
            return []

    real = sqlite3.connect(path)
    real.execute("ALTER TABLE t ADD COLUMN x TEXT")  # the other process won
    real.commit()
    assert add_column_if_missing(Racy(real), "t", "x", "ALTER TABLE t ADD COLUMN x TEXT") is False
    assert cols(path) == ["id", "x"]


def test_a_locked_database_is_waited_out_and_then_the_column_is_added(path):
    # check_same_thread=False: the timer thread below releases the lock, as another process would
    holder = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # another process mid-write
    migrator = sqlite3.connect(path, timeout=0.05)  # gives up on the lock almost at once
    threading.Timer(0.4, lambda: holder.execute("COMMIT")).start()
    started = time.monotonic()
    assert add_column_if_missing(migrator, "t", "x", "ALTER TABLE t ADD COLUMN x TEXT", attempts=6, wait_seconds=0.2) is True
    assert time.monotonic() - started >= 0.2  # it really did wait and retry
    migrator.commit()
    assert "x" in cols(path)


def test_a_database_that_stays_locked_raises_after_the_attempts_are_spent(path):
    holder = sqlite3.connect(path, timeout=5, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        migrator = sqlite3.connect(path, timeout=0.05)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            add_column_if_missing(migrator, "t", "x", "ALTER TABLE t ADD COLUMN x TEXT", attempts=2, wait_seconds=0.05)
    finally:
        holder.execute("ROLLBACK")


def test_other_errors_are_not_swallowed(path):
    c = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        add_column_if_missing(c, "t", "x", "ALTER TABLE not_a_table ADD COLUMN x TEXT")


def test_the_real_migrations_use_it_and_survive_four_processes_starting_together(tmp_path):
    """The actual incident: ensure_index_schema from several threads on a database
    that predates the columns."""
    from lisan.tools.db import connect
    from lisan.tools.rebuild_index import ensure_index_schema

    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
              "started TEXT NOT NULL, finished TEXT, exit_status TEXT, error TEXT)")
    c.execute("INSERT INTO task_runs (task_id, attempt, started) VALUES ('t', 1, 'x')")
    c.commit()
    c.close()
    errors = []
    barrier = threading.Barrier(4)

    def start_service():
        barrier.wait()
        try:
            conn = connect(db)
            ensure_index_schema(conn)
            conn.commit()
            conn.close()
        except Exception as exc:  # the old behaviour: one of them died here
            errors.append(repr(exc))

    threads = [threading.Thread(target=start_service) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    check = sqlite3.connect(db)
    assert "origin" in [r[1] for r in check.execute("PRAGMA table_info(task_runs)")]
    assert check.execute("SELECT origin FROM task_runs").fetchone()[0] == "adjutant"  # old rows belong to the adjutant
