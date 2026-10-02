"""The shared run ledger: one task_runs table, ownership by `origin`."""
from __future__ import annotations

import sqlite3

from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_runner import reclaim_stale_runs
from lisan.tools.rebuild_index import ensure_index_schema
from lisan.tools.run_ledger import ORIGIN_ADJUTANT, ORIGIN_PLAN, begin_run, ensure_table, finish_run

LEGACY_DDL = """CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    started TEXT NOT NULL, finished TEXT, exit_status TEXT, error TEXT)"""


def test_legacy_databases_gain_origin_and_old_rows_belong_to_the_adjutant(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.execute(LEGACY_DDL)
    conn.execute("INSERT INTO task_runs (task_id, attempt, started) VALUES ('t1', 1, '2020-01-01T00:00:00Z')")
    ensure_table(conn)
    ensure_table(conn)  # idempotent
    assert conn.execute("SELECT origin FROM task_runs").fetchall() == [("adjutant",)]


def test_begin_and_finish_round_trip(tmp_path):
    db = tmp_path / "ledger.sqlite"
    run_id = begin_run(db, "plan.x#step1", 1, origin=ORIGIN_PLAN)
    assert run_id is not None
    finish_run(db, run_id, ok=False, error="boom")
    row = sqlite3.connect(db).execute("SELECT task_id, attempt, exit_status, error, origin FROM task_runs").fetchone()
    assert row == ("plan.x#step1", 1, "failed", "boom", "plan")


def test_ledger_failures_never_raise(tmp_path):
    assert begin_run(tmp_path / "no" / "such" / "dir" / "x.sqlite", "t", 1, origin=ORIGIN_PLAN) is None
    finish_run(tmp_path, None, ok=True)  # no run id: a no-op


def test_adjutant_reclaim_leaves_a_long_running_plan_step_alone(tmp_path):
    """The job worker owns plan rows; a step may run for its whole timeout."""
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    db = tmp_path / "x.sqlite"
    conn = sqlite3.connect(db)
    ensure_index_schema(conn)
    conn.execute(
        "INSERT INTO task_runs (task_id, attempt, started, origin) VALUES ('plan.a#step1', 1, '2020-01-01T00:00:00Z', ?)",
        (ORIGIN_PLAN,),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, attempt, started, origin) VALUES ('loop-1', 1, '2020-01-01T00:00:00Z', ?)",
        (ORIGIN_ADJUTANT,),
    )
    conn.commit()
    reclaim_stale_runs(conn, vault, db, stale_seconds=900)
    rows = dict(conn.execute("SELECT task_id, finished IS NOT NULL FROM task_runs").fetchall())
    assert rows == {"plan.a#step1": 0, "loop-1": 1}  # only the Adjutant's row was closed
