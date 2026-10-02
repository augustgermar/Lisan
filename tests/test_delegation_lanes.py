"""Delegation step 3: lanes, the atomic concurrency cap, and crash recovery."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lisan.tools.delegation import OVERDUE_GRACE_SECONDS, reap_overdue_delegations
from lisan.tools.jobs import (
    claim_next_job,
    enqueue_job,
    get_job,
    list_jobs,
    mark_job_succeeded,
    reclaim_stale_running_jobs,
    set_child_pid,
)
from lisan.tools.scheduler import run_scheduler_loop


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LISAN_CODEX_TIMEOUT", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(vault=vault, db=tmp_path / "jobs.sqlite", tmp=tmp_path)


def _child(env, n=1):
    return enqueue_job(
        "agent.delegate", {"delegation_id": f"d{n}", "brief": "b", "timeout_seconds": 60}, max_attempts=1, db_path=env.db
    )


# ── the atomic cap ──────────────────────────────────────────────────────────

def test_type_limit_caps_running_children_inside_the_claim(env):
    for n in range(5):
        _child(env, n)
    limits = {"agent.delegate": 2}
    first = claim_next_job("w1", db_path=env.db, type_limits=limits)
    second = claim_next_job("w2", db_path=env.db, type_limits=limits)
    assert first and second
    assert claim_next_job("w3", db_path=env.db, type_limits=limits) is None  # cap reached
    mark_job_succeeded(first["id"], result={}, db_path=env.db)
    assert claim_next_job("w3", db_path=env.db, type_limits=limits) is not None  # a slot freed


def test_type_limit_does_not_block_other_types(env):
    _child(env)
    enqueue_job("task.reminder", {"text": "water the plants"}, db_path=env.db)
    # fill the one delegate slot (a reminder outranks it, so ask for it by type)
    assert claim_next_job("w1", db_path=env.db, job_types={"agent.delegate"})["job_type"] == "agent.delegate"
    _child(env, 2)  # a second child is now waiting, blocked by the cap
    other = claim_next_job("w2", db_path=env.db, type_limits={"agent.delegate": 1})
    assert other["job_type"] == "task.reminder"  # the capped type is skipped, not the whole queue
    assert claim_next_job("w3", db_path=env.db, type_limits={"agent.delegate": 1}) is None


def test_the_cap_holds_under_racing_claimers(env):
    for n in range(12):
        _child(env, n)
    claimed = []
    barrier = threading.Barrier(8)

    def racer(i):
        barrier.wait()
        job = claim_next_job(f"w{i}", db_path=env.db, type_limits={"agent.delegate": 3})
        if job:
            claimed.append(job["id"])

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 3 == len(set(claimed))


# ── lanes ───────────────────────────────────────────────────────────────────

def _run_loop(env, stop):
    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs=dict(vault=env.vault, db_path=env.db, poll_seconds=0.2, stop_event=stop),
        daemon=True,
    )
    thread.start()
    return thread


def _wait(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_long_codex_job_no_longer_blocks_a_reminder(env):
    """Before lanes, one 30-minute plan step delayed everything behind it."""
    release = threading.Event()
    long_running = threading.Event()

    def fake_dispatch(job, **kw):
        if job["job_type"] == "task.run_codex":
            long_running.set()
            release.wait(15)
        return {"ok": True}

    enqueue_job("task.run_codex", {"task": "build the thing"}, db_path=env.db)
    stop = threading.Event()
    with patch("lisan.tools.jobs.dispatch_job", fake_dispatch):
        loop = _run_loop(env, stop)
        try:
            assert long_running.wait(10), "the long job never started"
            reminder = enqueue_job("task.reminder", {"text": "water the plants"}, db_path=env.db)
            assert _wait(lambda: get_job(reminder, db_path=env.db)["status"] == "succeeded"), (
                "the reminder was stuck behind the long job"
            )
            assert [j["status"] for j in list_jobs(limit=10, db_path=env.db) if j["job_type"] == "task.run_codex"] == ["running"]
        finally:
            release.set()
            stop.set()
            loop.join(timeout=10)


def test_children_run_in_parallel_up_to_the_cap_and_no_further(env):
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fake_dispatch(job, **kw):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.4)
        with lock:
            state["now"] -= 1
        return {"status": "succeeded"}

    ids = [_child(env, n) for n in range(7)]
    stop = threading.Event()
    with patch("lisan.tools.jobs.dispatch_job", fake_dispatch):
        loop = _run_loop(env, stop)
        try:
            assert _wait(lambda: all(get_job(i, db_path=env.db)["status"] == "succeeded" for i in ids), 30)
        finally:
            stop.set()
            loop.join(timeout=10)
    assert state["peak"] == 3  # parallel, and exactly delegation.max_concurrent


def test_lanes_can_be_turned_off_and_the_main_lane_takes_everything(env):
    seen = []
    enqueue_job("task.run_codex", {"task": "x"}, db_path=env.db)

    def fake_dispatch(job, **kw):
        seen.append((job["job_type"], threading.current_thread().name))
        return {"ok": True}

    with patch("lisan.tools.jobs.dispatch_job", fake_dispatch):
        run_scheduler_loop(vault=env.vault, db_path=env.db, poll_seconds=0.1, max_ticks=1, lanes=False)
    assert [t for t, _ in seen] == ["task.run_codex"]


# ── crash recovery ──────────────────────────────────────────────────────────

def _age(env, job_id, seconds_ago):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))
    conn = sqlite3.connect(env.db)
    conn.execute("UPDATE jobs SET started_at = ? WHERE id = ?", (stamp, job_id))
    conn.commit()
    conn.close()


def _running_child(env, *, age):
    job_id = _child(env)
    claim_next_job("dead-worker", db_path=env.db)
    _age(env, job_id, age)
    return job_id


def _orphan(tmp_path, name):
    script = tmp_path / name
    script.write_text("#!/bin/sh\nsleep 63\n", encoding="utf-8")
    script.chmod(0o755)
    return subprocess.Popen([str(script)], start_new_session=True)


def test_an_overdue_child_is_failed_not_requeued_and_its_orphan_is_killed(env, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "fake_codex_for_reaper.sh")
    job_id = _running_child(env, age=60 + OVERDUE_GRACE_SECONDS + 30)
    proc = _orphan(env.tmp, "fake_codex_for_reaper.sh")
    set_child_pid(job_id, proc.pid, db_path=env.db)
    try:
        with patch("lisan.tools.escalation._notify_owner", return_value=True) as notify:
            assert reap_overdue_delegations(env.db, vault=env.vault) == ["d1"]
        proc.wait(timeout=5)  # killed
        job = get_job(job_id, db_path=env.db)
        assert job["status"] == "failed"
        assert "died mid-run" in job["error"] and "not retried" in job["error"]
        assert "orphaned process was killed" in job["error"]
        assert "not retried" in notify.call_args.args[0]
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_recycled_pid_that_is_not_codex_is_never_killed(env, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "fake_codex_for_reaper.sh")
    job_id = _running_child(env, age=60 + OVERDUE_GRACE_SECONDS + 30)
    bystander = _orphan(env.tmp, "unrelated_daemon.sh")  # owns the recorded pid now
    set_child_pid(job_id, bystander.pid, db_path=env.db)
    try:
        with patch("lisan.tools.escalation._notify_owner", return_value=True):
            reap_overdue_delegations(env.db, vault=env.vault)
        assert bystander.poll() is None  # still alive
        assert get_job(job_id, db_path=env.db)["status"] == "failed"  # the job is still reaped
    finally:
        bystander.kill()


def test_a_child_inside_its_timeout_is_left_alone(env):
    job_id = _running_child(env, age=30)  # timeout is 60
    assert reap_overdue_delegations(env.db, vault=env.vault) == []
    assert get_job(job_id, db_path=env.db)["status"] == "running"


def test_generic_stale_reclaim_never_requeues_a_child(env):
    child = _running_child(env, age=3 * 3600)
    other = enqueue_job("task.reminder", {"text": "x"}, db_path=env.db)
    claim_next_job("dead-worker", db_path=env.db, job_types={"task.reminder"})
    _age(env, other, 3 * 3600)
    assert reclaim_stale_running_jobs(env.db) == 1  # only the reminder
    assert get_job(child, db_path=env.db)["status"] == "running"
    assert get_job(other, db_path=env.db)["status"] == "queued"
