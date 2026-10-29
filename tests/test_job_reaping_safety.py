"""Crash recovery never repeats an unknown side effect, and Adjutant
cycles never overlap."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from lisan.tools.jobs import claim_next_job, enqueue_job, get_job, reap_stuck_jobs, reclaim_stale_running_jobs


def _run_and_age(db: Path, job_type: str, minutes_ago: int) -> str:
    from datetime import datetime, timedelta, timezone

    job_id = enqueue_job(job_type, {"task": "x"}, db_path=db)
    claim_next_job("dead-worker", db_path=db, job_types={job_type})
    started = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (started, job_id))
    conn.commit()
    conn.close()
    return job_id


def test_reaper_does_not_requeue_a_codex_run(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = _run_and_age(db, "task.run_codex", 120)
    result = reap_stuck_jobs(db_path=db, timeout_minutes=15, retry=True)
    assert result["reaped_count"] == 1
    row = get_job(job, db_path=db)
    assert row["status"] == "failed"
    assert "side effects are unknown" in row["error"]


def test_reaper_gives_long_jobs_their_full_wall_limit(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = _run_and_age(db, "task.run_codex", 30)  # codex may run 40 minutes
    assert reap_stuck_jobs(db_path=db, timeout_minutes=15)["reaped_count"] == 0
    assert get_job(job, db_path=db)["status"] == "running"


def test_reaper_still_retries_idempotent_jobs(tmp_path):
    db = tmp_path / "jobs.sqlite"
    job = _run_and_age(db, "analyst.scan", 30)
    reap_stuck_jobs(db_path=db, timeout_minutes=15, retry=True)
    assert get_job(job, db_path=db)["status"] == "retry_wait"


def test_stale_reclaim_fails_rather_than_reruns_unsafe_jobs(tmp_path):
    db = tmp_path / "jobs.sqlite"
    unsafe = _run_and_age(db, "task.prompt", 180)
    safe = _run_and_age(db, "index.rebuild_record", 180)
    assert reclaim_stale_running_jobs(db) == 1
    assert get_job(safe, db_path=db)["status"] == "queued"
    row = get_job(unsafe, db_path=db)
    assert row["status"] == "failed" and "side effects are unknown" in row["error"]


def test_a_second_adjutant_cycle_is_skipped_while_one_runs(tmp_path, monkeypatch):
    import fcntl

    from lisan.tools import adjutant_runner

    vault = tmp_path / "vault"
    vault.mkdir()
    called = []
    monkeypatch.setattr(adjutant_runner, "_run_cycle_unlocked", lambda *a, **k: called.append(1) or {"halted": False})
    with open(vault / adjutant_runner.CYCLE_LOCK_NAME, "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = adjutant_runner.run_cycle(vault, tmp_path / "db.sqlite")
        assert result["skipped"]
        assert called == []
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    assert adjutant_runner.run_cycle(vault, tmp_path / "db.sqlite") == {"halted": False}
    assert called == [1]
