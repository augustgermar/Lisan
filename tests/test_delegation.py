"""Delegated workers (docs/delegation_workorder.md, steps 1-2).

Claims: authority never grows down the tree; a child is one durable,
never-auto-retried job; its brief and result are kept; cancel really kills a
running child and the worker does not overwrite that with "failed".
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lisan.providers.base import LLMResponse, ProviderError
from lisan.tools import delegation
from lisan.tools.delegation import (
    MAX_TIMEOUT_SECONDS,
    cancel_delegation,
    delegate,
    list_delegations,
    run_delegation,
    show_delegation,
)
from lisan.tools.jobs import claim_next_job, enqueue_job, get_job, list_jobs, run_jobs_worker

FULL = "danger-full-access"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LISAN_CODEX_TIMEOUT", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(vault=vault, db=tmp_path / "jobs.sqlite", tmp=tmp_path)


def _delegate(env, brief="inspect the box", **kw):
    kw.setdefault("profile_ceiling", FULL)
    kw.setdefault("config", {})
    return delegate(brief, vault=env.vault, db_path=env.db, working_directory=str(env.tmp), **kw)


# ── validation and authority ────────────────────────────────────────────────

def test_a_child_defaults_to_its_parents_authority_and_is_never_retried(env):
    out = _delegate(env)
    assert out["profile"] == "full"
    job = get_job(out["job_id"], db_path=env.db)
    assert job["job_type"] == "agent.delegate" and job["status"] == "queued"
    assert job["max_attempts"] == 1
    assert job["payload"]["brief"] == "inspect the box"  # the brief is kept
    assert job["payload"]["parent"] == {"kind": "cli"}


def test_a_child_can_never_exceed_its_parents_profile(env):
    with pytest.raises(ValueError, match="exceeds the delegating caller's authority"):
        _delegate(env, profile="full", profile_ceiling="workspace-write")
    out = _delegate(env, profile="read_only", profile_ceiling="workspace-write")
    assert out["profile"] == "read_only"
    out = _delegate(env, profile_ceiling="read-only")
    assert out["profile"] == "read_only"  # default is the parent's own level


@pytest.mark.parametrize("bad", [0, -1, MAX_TIMEOUT_SECONDS + 1])
def test_timeout_is_bounded_below_the_queues_stale_job_reclaim(env, bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        _delegate(env, timeout_seconds=bad)
    assert MAX_TIMEOUT_SECONDS < 45 * 60  # the whole point of the ceiling


def test_empty_brief_unknown_profile_and_relative_dir_are_refused(env):
    with pytest.raises(ValueError, match="needs a brief"):
        _delegate(env, brief="  ")
    with pytest.raises(ValueError, match="unknown profile"):
        _delegate(env, profile="root")
    with pytest.raises(ValueError, match="absolute"):
        delegate("x", vault=env.vault, db_path=env.db, working_directory="relative/dir",
                 profile_ceiling=FULL, config={})


def test_outstanding_cap_refuses_the_next_child(env):
    config = {"delegation": {"max_outstanding": 2}}
    _delegate(env, config=config)
    _delegate(env, config=config)
    with pytest.raises(ValueError, match="already queued or running"):
        _delegate(env, config=config)


def test_intent_never_rule_refuses_at_enqueue_and_again_at_run(env):
    deny = (SimpleNamespace(decision="deny", rule="never: destructive", reasons=["no deletes"]), 3)
    with patch("lisan.tools.execution_tools._chat_intent_verdict", return_value=deny):
        with pytest.raises(ValueError, match="forbids this"):
            _delegate(env)
    out = _delegate(env)  # allowed when queued...
    job = get_job(out["job_id"], db_path=env.db)
    with patch("lisan.tools.execution_tools._chat_intent_verdict", return_value=deny):
        with pytest.raises(RuntimeError, match="refused: intent.md"):
            run_delegation(job, vault=env.vault, db_path=env.db, config={})  # ...but intent may change


# ── running a child ─────────────────────────────────────────────────────────

def _ledger(env):
    conn = sqlite3.connect(env.db)
    try:
        return conn.execute("SELECT task_id, attempt, exit_status, origin FROM task_runs ORDER BY id").fetchall()
    finally:
        conn.close()


def test_a_child_runs_with_its_scoped_sandbox_timeout_and_no_memory(env):
    out = _delegate(env, profile="read_only", timeout_seconds=77)
    job = get_job(out["job_id"], db_path=env.db)
    seen = {}

    def fake_complete(self, prompt, **kw):
        seen.update(kw, prompt=prompt)
        return LLMResponse(text="all quiet", provider="stub", model="s")

    with patch("lisan.providers.codex.CodexClient.complete", fake_complete):
        result = run_delegation(job, vault=env.vault, db_path=env.db, config={})
    assert seen["sandbox_mode"] == "read-only"
    assert seen["timeout_seconds"] == 77
    assert "inspect the box" in seen["prompt"]
    assert "memory" in seen["prompt"]  # told it has none; the briefing carries no assembled context
    assert result["status"] == "succeeded" and result["text"] == "all quiet"
    assert _ledger(env) == [(out["delegation_id"], 1, "ok", "delegate")]


def test_structured_result_is_parsed_when_a_schema_is_given(env):
    schema = {"type": "object", "properties": {"disks": {"type": "integer"}}}
    out = _delegate(env, result_schema=schema)
    job = get_job(out["job_id"], db_path=env.db)
    with patch("lisan.providers.codex.CodexClient.complete",
               lambda self, prompt, **kw: LLMResponse(text='{"disks": 4}', provider="s", model="s")):
        result = run_delegation(job, vault=env.vault, db_path=env.db, config={})
    assert result["data"] == {"disks": 4}


def test_a_failed_child_raises_and_is_never_requeued(env):
    out = _delegate(env)
    with patch("lisan.providers.codex.CodexClient.complete", side_effect=ProviderError("exec failed")), \
            patch("lisan.tools.escalation._notify_owner", return_value=True) as notify:
        run_jobs_worker(vault=env.vault, db_path=env.db, job_types={"agent.delegate"})
    job = get_job(out["job_id"], db_path=env.db)
    assert job["status"] == "failed" and "exec failed" in job["error"]
    assert [j for j in list_jobs(limit=50, db_path=env.db) if j["job_type"] == "agent.delegate"] == [job]
    # the owner is told the real cause and that nothing was retried
    text = notify.call_args.args[0]
    assert "exec failed" in text and "not retried" in text
    assert _ledger(env)[0][2:] == ("failed", "delegate")


# ── cancel really kills ─────────────────────────────────────────────────────

def _fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.sh"
    script.write_text("#!/bin/sh\ncat >/dev/null\nsleep 62\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(script))


def _alive(pid):
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_cancel_kills_a_running_child_and_the_job_stays_canceled(env, monkeypatch):
    _fake_codex(env.tmp, monkeypatch)
    out = _delegate(env, timeout_seconds=120)
    worker = threading.Thread(
        target=lambda: run_jobs_worker(vault=env.vault, db_path=env.db, job_types={"agent.delegate"}),
        daemon=True,
    )
    worker.start()
    pid = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not pid:
        pid = (get_job(out["job_id"], db_path=env.db) or {}).get("child_pid")
        time.sleep(0.1)
    assert pid, "the running child's pid was never recorded"
    assert _alive(pid)

    assert cancel_delegation(out["delegation_id"], db_path=env.db) is True
    worker.join(timeout=15)
    assert not worker.is_alive()
    time.sleep(0.3)
    assert not _alive(pid)
    job = get_job(out["job_id"], db_path=env.db)
    assert job["status"] == "canceled"  # not overwritten with "failed"
    assert job.get("child_pid") in (None, 0)
    assert cancel_delegation(out["delegation_id"], db_path=env.db) is False  # nothing left to cancel


def test_cancel_a_queued_child_and_unknown_ids(env):
    out = _delegate(env)
    assert cancel_delegation(out["delegation_id"], db_path=env.db) is True
    assert get_job(out["job_id"], db_path=env.db)["status"] == "canceled"
    assert cancel_delegation("deleg.nope", db_path=env.db) is False


# ── visibility ──────────────────────────────────────────────────────────────

def test_list_and_show(env):
    out = _delegate(env, brief="audit ssh config")
    listed = list_delegations(db_path=env.db)
    assert [d["delegation_id"] for d in listed] == [out["delegation_id"]]
    shown = show_delegation(out["delegation_id"], db_path=env.db)
    assert shown["brief"] == "audit ssh config" and shown["status"] == "queued"
    assert show_delegation("deleg.nope", db_path=env.db) is None
    assert "audit ssh config" in delegation.format_delegations(listed)


# ── lane filtering (used by the scheduler, step 3) ──────────────────────────

def test_claim_next_job_can_exclude_types(env):
    enqueue_job("agent.delegate", {"delegation_id": "d1", "brief": "b"}, max_attempts=1, db_path=env.db)
    enqueue_job("task.reminder", {"text": "water the plants"}, db_path=env.db)
    first = claim_next_job("w", db_path=env.db, exclude_job_types={"agent.delegate"})
    assert first["job_type"] == "task.reminder"
    assert claim_next_job("w", db_path=env.db, exclude_job_types={"agent.delegate"}) is None
    only = claim_next_job("w", db_path=env.db, job_types={"agent.delegate"})
    assert only["job_type"] == "agent.delegate"
