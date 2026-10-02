"""Delegation steps 4-5: durable fan-out/join, plan fanout steps, chat delegate.

The properties that matter: a join fires exactly once and survives a crash at
any point; a re-run of a half-launched fan-out cannot double-launch; a plan
waiting on workers is visible, cancellable and resumable; the chat tool never
returns anything that reads like a result and never works from inside a plan.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lisan.providers.base import LLMResponse, ProviderError
from lisan.tools import delegation, plans
from lisan.tools.delegation import (
    cancel_group,
    child_job_ids_for,
    get_group,
    group_children,
    group_summary_message,
    launch_group,
    settle_groups,
)
from lisan.tools.execution_tools import delegate_tool
from lisan.tools.jobs import cancel_job, enqueue_job, get_job, list_jobs, run_jobs_worker
from lisan.tools.plans import cancel_plan, create_plan, format_plans, list_plans, resume_plan

FULL = "danger-full-access"
CFG: dict = {}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("LISAN_CODEX_TIMEOUT", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(vault=vault, db=tmp_path / "jobs.sqlite", tmp=tmp_path)


class World:
    """Everything a drain touches that must not reach the network or an LLM."""

    def __init__(self):
        self.prompts: list[str] = []
        self.delivered: list[str] = []
        self.captured: list[dict] = []

    def fake_codex(self, prompt, **kw):
        self.prompts.append(prompt)
        if "You are a delegated worker" in prompt:
            if "FAIL" in prompt:
                raise ProviderError("worker blew up")
            task = prompt.split("TASK:\n", 1)[1].strip()
            return LLMResponse(text=f"done: {task[:40]}", provider="stub", model="s")
        return LLMResponse(text="plan step ran", provider="stub", model="s")


@contextmanager
def world(*, real_report=False):
    w = World()
    stack = [
        patch("lisan.providers.codex.CodexClient"),
        patch("lisan.tools.scheduler._deliver_owner_message", side_effect=lambda text, **kw: w.delivered.append(text)),
        patch("lisan.tools.capture.capture_text", side_effect=lambda **kw: w.captured.append(kw) or {"captured": True}),
        patch("lisan.tools.plans.load_config", return_value={}),
        patch("lisan.tools.execution_tools.assemble_context", return_value="(ctx)"),
        patch("lisan.tools.escalation._notify_owner", return_value=True),
    ]
    if not real_report:
        stack.append(patch("lisan.tools.plans._report_to_memory"))
    mocks = [p.start() for p in stack]
    mocks[0].return_value.complete.side_effect = w.fake_codex
    try:
        yield w
    finally:
        for p in reversed(stack):
            p.stop()


def drain(env, **kw):
    return run_jobs_worker(vault=env.vault, db_path=env.db, **kw)


def specs(*briefs, **extra):
    return [{"brief": b, "profile": "read_only", **extra} for b in briefs]


def launch(env, briefs=("a", "b"), group_id="grp.t1", continuation=None, **kw):
    return launch_group(
        specs(*briefs),
        group_id=group_id, kind="chat", parent={"kind": "chat"},
        continuation_type="agent.delegate_report",
        continuation_payload=continuation or {"group_id": group_id, "goal": "g"},
        ceiling=FULL, config=CFG, vault=env.vault, db_path=env.db, **kw,
    )


def children_jobs(env):
    return [j for j in list_jobs(limit=500, db_path=env.db) if j["job_type"] == "agent.delegate"]


# ── enqueue idempotence ─────────────────────────────────────────────────────

def test_enqueue_with_an_explicit_id_is_idempotent(env):
    first = enqueue_job("writer.extract_turn", {"marker": "x"}, job_id="job.fixed", db_path=env.db)
    again = enqueue_job("writer.extract_turn", {"marker": "DIFFERENT"}, job_id="job.fixed", db_path=env.db)
    assert first == again == "job.fixed"
    jobs = list_jobs(limit=10, db_path=env.db)
    assert len(jobs) == 1 and jobs[0]["payload"]["marker"] == "x"  # the original is untouched


# ── launching a group ───────────────────────────────────────────────────────

def test_launch_validates_everything_before_queuing_anything(env):
    bad = specs("fine") + [{"brief": "  "}]
    with pytest.raises(ValueError, match="needs a brief"):
        launch_group(bad, group_id="g", kind="chat", parent={}, continuation_type="agent.delegate_report",
                     continuation_payload={}, ceiling=FULL, config=CFG, vault=env.vault, db_path=env.db)
    assert children_jobs(env) == [] and get_group("g", db_path=env.db) is None


def test_launch_enforces_max_children_ceiling_and_outstanding_cap(env):
    with pytest.raises(ValueError, match="too many children"):
        launch(env, briefs=[f"c{i}" for i in range(7)])
    with pytest.raises(ValueError, match="exceeds the delegating caller's authority"):
        launch_group([{"brief": "b", "profile": "full"}], group_id="g2", kind="chat", parent={},
                     continuation_type="agent.delegate_report", continuation_payload={},
                     ceiling="read-only", config=CFG, vault=env.vault, db_path=env.db)
    tight = {"delegation": {"max_outstanding": 3}}
    launch_group(specs("a", "b"), group_id="g3", kind="chat", parent={}, continuation_type="agent.delegate_report",
                 continuation_payload={}, ceiling=FULL, config=tight, vault=env.vault, db_path=env.db)
    with pytest.raises(ValueError, match="would exceed"):
        launch_group(specs("c", "d"), group_id="g4", kind="chat", parent={}, continuation_type="agent.delegate_report",
                     continuation_payload={}, ceiling=FULL, config=tight, vault=env.vault, db_path=env.db)


def test_relaunching_a_half_launched_group_cannot_double_launch(env):
    first = launch(env)
    second = launch(env)  # e.g. the launching job was retried after a crash
    assert first["child_job_ids"] == second["child_job_ids"] == child_job_ids_for("grp.t1", 2)
    assert len(children_jobs(env)) == 2
    conn = sqlite3.connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM delegation_groups").fetchone()[0] == 1


# ── settling: exactly once, crash-safe ──────────────────────────────────────

def _finish(env, job_id, status="succeeded", text="ok"):
    conn = sqlite3.connect(env.db)
    conn.execute("UPDATE jobs SET status = ?, result_json = ? WHERE id = ?",
                 (status, json.dumps({"text": text, "duration_s": 1.0}), job_id))
    conn.commit()
    conn.close()


def _continuations(env):
    return [j for j in list_jobs(limit=500, db_path=env.db) if j["id"].startswith("job.join.")]


def test_a_group_settles_only_when_every_child_has_finished(env):
    out = launch(env)
    assert settle_groups(env.db) == [] and _continuations(env) == []
    _finish(env, out["child_job_ids"][0])
    assert settle_groups(env.db) == []  # one child still out
    _finish(env, out["child_job_ids"][1], status="failed")
    assert settle_groups(env.db) == ["grp.t1"]
    assert len(_continuations(env)) == 1
    assert get_group("grp.t1", db_path=env.db)["state"] == "settled"


def test_settling_twice_enqueues_the_continuation_once(env):
    out = launch(env)
    for job_id in out["child_job_ids"]:
        _finish(env, job_id)
    assert settle_groups(env.db) == ["grp.t1"]
    assert settle_groups(env.db) == []  # already settled
    assert len(_continuations(env)) == 1


def test_a_crash_between_enqueue_and_mark_is_repaired_by_the_next_sweep(env):
    out = launch(env)
    for job_id in out["child_job_ids"]:
        _finish(env, job_id)
    # the crash: continuation enqueued, group never marked settled
    enqueue_job("agent.delegate_report", {"group_id": "grp.t1"}, job_id="job.join.grp.t1", db_path=env.db)
    assert get_group("grp.t1", db_path=env.db)["state"] == "waiting"
    assert settle_groups(env.db) == ["grp.t1"]
    assert len(_continuations(env)) == 1  # no duplicate
    assert get_group("grp.t1", db_path=env.db)["state"] == "settled"


def test_canceled_and_vanished_children_count_as_finished(env):
    out = launch(env, briefs=("a", "b", "c"))
    _finish(env, out["child_job_ids"][0])
    cancel_job(out["child_job_ids"][1], db_path=env.db)
    conn = sqlite3.connect(env.db)
    conn.execute("DELETE FROM jobs WHERE id = ?", (out["child_job_ids"][2],))
    conn.commit()
    conn.close()
    assert settle_groups(env.db) == ["grp.t1"]
    statuses = [c["status"] for c in group_children("grp.t1", db_path=env.db)]
    assert statuses == ["succeeded", "canceled", "missing"]


def test_cancel_group_stops_children_and_prevents_the_continuation(env):
    out = launch(env)
    assert cancel_group("grp.t1", db_path=env.db) == 2
    assert [get_job(i, db_path=env.db)["status"] for i in out["child_job_ids"]] == ["canceled", "canceled"]
    assert get_group("grp.t1", db_path=env.db)["state"] == "canceled"
    assert settle_groups(env.db) == [] and _continuations(env) == []


# ── the worker joins groups: fast path and guarantee ────────────────────────

def test_the_worker_runs_children_then_the_report_in_one_drain(env):
    launch(env, briefs=("look at A", "look at B"))
    with world() as w:
        drain(env)
    assert len(w.delivered) == 1  # one message for the whole group
    assert "done: look at A" in w.delivered[0] and "done: look at B" in w.delivered[0]
    assert len(w.captured) == 1 and "delegation" in w.captured[0]["text"]  # one capture turn
    assert get_group("grp.t1", db_path=env.db)["state"] == "settled"


def test_if_the_fast_path_is_lost_the_next_drain_still_joins_the_group(env):
    launch(env, briefs=("A", "B"))
    with world() as w:
        with patch("lisan.tools.jobs._settle_delegation_groups"):  # the hook never fires
            drain(env)
        assert w.delivered == [] and _continuations(env) == []
        drain(env)  # the sweep at the start of any worker run is the guarantee
    assert len(w.delivered) == 1 and len(_continuations(env)) == 1


def test_max_jobs_is_honoured_even_when_a_job_was_canceled_mid_run(env):
    """Regression: the canceled branches used to `continue`, skipping the max_jobs check."""
    enqueue_job("task.reminder", {"text": "one"}, db_path=env.db)
    enqueue_job("task.reminder", {"text": "two"}, db_path=env.db)

    def cancel_self(job, **kw):
        cancel_job(job["id"], db_path=env.db)
        return {"ok": True}

    with patch("lisan.tools.jobs.dispatch_job", cancel_self):
        summary = drain(env, max_jobs=1)
    assert summary["processed_count"] == 1
    assert sorted(j["status"] for j in list_jobs(limit=10, db_path=env.db)) == ["canceled", "queued"]


# ── the report ──────────────────────────────────────────────────────────────

def test_report_shows_every_childs_outcome_and_real_errors():
    children = [
        {"status": "succeeded", "brief": "audit the ssh config\nsecond line", "profile": "read_only",
         "duration_s": 12.0, "text": "found 2 issues", "delegation_id": "deleg.a"},
        {"status": "failed", "brief": "audit the backups", "error": "coding agent timed out after 900s", "delegation_id": "deleg.b"},
    ]
    text = group_summary_message("audit the server", children)
    assert text.startswith("⚠️ Delegated work finished: audit the server (1 of 2 succeeded)")
    assert "1. ✓ audit the ssh config (read_only, 12.0s)" in text and "found 2 issues" in text
    assert "second line" not in text  # first line of the brief only
    assert "2. ✗ audit the backups — failed: coding agent timed out after 900s" in text
    assert "lisan delegate show deleg.a (and the others)" in text
    assert group_summary_message("g", [dict(children[0])]).startswith("✅")


def test_report_truncates_long_results_and_survives_failing_capture_and_delivery(env):
    out = launch(env, briefs=("A",))
    _finish(env, out["child_job_ids"][0], text="x" * 5000)
    settle_groups(env.db)
    job = {"payload": {"group_id": "grp.t1", "goal": "g"}}
    sent = []

    def boom(**kw):
        raise RuntimeError("pipeline down")

    result = delegation.run_delegation_report(job, vault=env.vault, db_path=env.db, send_fn=lambda t, c: sent.append(t), capture=boom)
    assert result["delivered"] is True and len(sent[0]) < 1500  # truncated, and capture failing did not matter

    def no_delivery(text, chat_id):
        raise RuntimeError("telegram down")

    result = delegation.run_delegation_report(job, vault=env.vault, db_path=env.db, send_fn=no_delivery, capture=lambda **kw: {})
    assert result["delivered"] is False  # reported as such, not raised, not hidden


def test_the_report_runs_on_the_long_lane():
    from lisan.tools.scheduler import LONG_LANE_TYPES

    assert "agent.delegate_report" in LONG_LANE_TYPES


# ── plan fanout: creation ───────────────────────────────────────────────────

def _fanout(*briefs, join="all", **kw):
    return {"kind": "fanout", "description": "check them all", "children": specs(*briefs), "join": join, **kw}


def _plan(env, steps, **kw):
    return create_plan(goal="fan out", steps=steps, db_path=env.db, config=kw.pop("config", CFG), **kw)


def test_fanout_is_validated_at_creation_not_at_run_time(env):
    for bad, match in [
        ({"kind": "fanout", "description": "d"}, "non-empty list of children"),
        ({"kind": "fanout", "description": "d", "children": []}, "non-empty list of children"),
        (_fanout(*[f"c{i}" for i in range(7)]), "too many children"),
        (_fanout("a", join="whenever"), "join must be one of"),
        (_fanout("a", retries=1), "cannot retry"),
        ({"kind": "fanout", "description": "d", "children": [{"brief": ""}]}, "step 1: a delegated task needs a brief"),
        ({"kind": "fanout", "description": "d", "children": [{"brief": "b", "timeout_seconds": 99999}]}, "timeout_seconds"),
    ]:
        with pytest.raises(ValueError, match=match):
            _plan(env, [bad])
    assert list_jobs(limit=10, db_path=env.db) == []  # nothing was queued


def test_fanout_children_cannot_exceed_the_plans_authority(env):
    read_only = {"providers": {"codex": {"sandbox_mode": "read-only"}}}
    with pytest.raises(ValueError, match="exceeds the delegating caller's authority"):
        _plan(env, [{"kind": "fanout", "description": "d", "children": [{"brief": "b", "profile": "full"}]}], config=read_only)
    summary = _plan(env, [_fanout("a")], config=read_only)  # default profile = the ceiling
    payload = get_job(summary["job_id"], db_path=env.db)["payload"]
    assert payload["profile_ceiling"] == "read-only"
    assert payload["steps"][0]["children"][0]["profile"] == "read_only"


# ── plan fanout: running ────────────────────────────────────────────────────

def test_a_plan_fans_out_joins_and_carries_on(env):
    summary = _plan(env, [
        {"kind": "note", "description": "start"},
        _fanout("look at A", "look at B"),
        {"kind": "codex", "description": "summarize what the workers found"},
    ])
    with world() as w:
        drain(env)
    plan = list_plans(db_path=env.db)[0]
    assert plan["steps_done"] == 3 and not plan["active"] and not plan["waiting"]
    assert len(children_jobs(env)) == 2 and all(j["status"] == "succeeded" for j in children_jobs(env))
    # the step after the join saw what the workers produced
    final_prompt = [p for p in w.prompts if "summarize what the workers found" in p][0]
    assert "2 of 2 workers succeeded" in final_prompt
    assert "done: look at A" in final_prompt and "done: look at B" in final_prompt
    assert "Plan completed" in w.delivered[-1]
    assert summary["plan_id"] == plan["plan_id"]


def test_a_plan_waiting_on_workers_is_visible_as_active(env):
    _plan(env, [_fanout("A", "B")])
    with world():
        drain(env, job_types={"plan.run"}, max_jobs=1)  # the launch only; children not yet run
    plan = list_plans(db_path=env.db)[0]
    assert plan["waiting"] and plan["active"]
    assert "waiting on workers" in format_plans([plan])
    assert len(children_jobs(env)) == 2 and all(j["status"] == "queued" for j in children_jobs(env))


def test_join_all_fails_the_plan_when_any_child_fails(env):
    _plan(env, [_fanout("fine", "FAIL this one"), {"kind": "note", "description": "never runs"}])
    with world() as w:
        drain(env)
    plan = list_plans(db_path=env.db)[0]
    assert plan["steps_done"] == 0
    assert "Plan failed" in w.delivered[-1]
    assert "1 of 2 workers succeeded" in w.delivered[-1] or "worker blew up" in w.delivered[-1]
    steps = get_job(plan["job_id"], db_path=env.db)["payload"]["steps"]
    assert steps[0]["status"] == "failed" and steps[1]["status"] == "skipped"
    assert "worker blew up" in steps[0]["result"]


def test_best_effort_continues_with_failures_listed(env):
    _plan(env, [_fanout("fine", "FAIL this one", join="best_effort"), {"kind": "codex", "description": "wrap up"}])
    with world() as w:
        drain(env)
    assert list_plans(db_path=env.db)[0]["steps_done"] == 2
    wrap = [p for p in w.prompts if "wrap up" in p][0]
    assert "1 of 2 workers succeeded" in wrap and "FAILED" in wrap and "worker blew up" in wrap


def test_best_effort_still_fails_if_every_child_failed(env):
    _plan(env, [_fanout("FAIL one", "FAIL two", join="best_effort")])
    with world() as w:
        drain(env)
    assert "Plan failed" in w.delivered[-1]


def test_a_refused_fanout_fails_the_step_honestly(env):
    deny = (SimpleNamespace(decision="deny", rule="never: destructive", reasons=["no"]), 3)
    _plan(env, [_fanout("A")])
    with world() as w, patch("lisan.tools.execution_tools._chat_intent_verdict", return_value=deny):
        drain(env)
    assert children_jobs(env) == []
    assert "Plan failed" in w.delivered[-1] and "could not start the children" in w.delivered[-1]


def test_a_retried_launch_job_does_not_double_launch(env):
    summary = _plan(env, [_fanout("A", "B")])
    stale = get_job(summary["job_id"], db_path=env.db)  # as the queue would hand it over again after a crash
    with world():
        plans.run_plan_step(stale, vault=env.vault, db_path=env.db, config=CFG)
        plans.run_plan_step(stale, vault=env.vault, db_path=env.db, config=CFG)
    assert len(children_jobs(env)) == 2
    conn = sqlite3.connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM delegation_groups").fetchone()[0] == 1


def test_a_duplicate_run_of_a_waiting_launch_is_a_noop(env):
    summary = _plan(env, [_fanout("A")])
    with world():
        drain(env, job_types={"plan.run"}, max_jobs=1)
        launch_job = get_job(summary["job_id"], db_path=env.db)  # payload now says "waiting"
        out = plans.run_plan_step(launch_job, vault=env.vault, db_path=env.db, config=CFG)
    assert out["status"] == "waiting" and len(children_jobs(env)) == 1


# ── plan fanout: cancel and resume ──────────────────────────────────────────

def test_canceling_a_waiting_plan_stops_its_workers_and_ends_it(env):
    summary = _plan(env, [_fanout("A", "B"), {"kind": "note", "description": "never"}])
    with world() as w:
        drain(env, job_types={"plan.run"}, max_jobs=1)
        assert cancel_plan(summary["plan_id"], db_path=env.db, vault=env.vault) is True
        drain(env)  # nothing may run now
    assert all(j["status"] == "canceled" for j in children_jobs(env))
    plan = list_plans(db_path=env.db)[0]
    assert not plan["active"] and not plan["waiting"]
    assert "Plan canceled" in w.delivered[-1]
    assert _continuations(env) == []
    assert cancel_plan(summary["plan_id"], db_path=env.db, vault=env.vault) is False


def test_resume_reruns_a_failed_fanout_as_a_fresh_group(env):
    summary = _plan(env, [_fanout("fine", "FAIL once")])
    with world() as w:
        drain(env)
        first_ids = {j["id"] for j in children_jobs(env)}
        assert "Plan failed" in w.delivered[-1]
        # the world is fixed: the failing brief now succeeds
        w.fake_codex_original = w.fake_codex
        resume_plan(summary["plan_id"], db_path=env.db)
        sources = [j for j in list_jobs(limit=50, db_path=env.db) if j["job_type"] == "plan.run" and j["status"] == "queued"]
        for job in sources:  # swap the failing brief for a good one before it launches
            for child in job["payload"]["steps"][0]["children"]:
                child["brief"] = child["brief"].replace("FAIL", "OK")
            conn = sqlite3.connect(env.db)
            conn.execute("UPDATE jobs SET payload_json = ? WHERE id = ?", (json.dumps(job["payload"]), job["id"]))
            conn.commit()
            conn.close()
        drain(env)
    second_ids = {j["id"] for j in children_jobs(env)} - first_ids
    assert len(second_ids) == 2  # a fresh group, not the old children
    plan = list_plans(db_path=env.db)[0]
    assert plan["steps_done"] == 1 and not plan["active"]
    assert "Plan completed" in w.delivered[-1]


# ── the chat tool ───────────────────────────────────────────────────────────

def _tool(env, tasks, **kw):
    kw.setdefault("conversation_id", "telegram-4242")
    return delegate_tool(tasks=tasks, goal=kw.pop("goal", "look around"), vault=env.vault, db_path=env.db, config=CFG, **kw)


def test_the_tool_returns_a_handle_that_never_reads_like_a_result(env):
    reply = _tool(env, [{"brief": "audit A", "profile": "read_only"}, {"brief": "audit B"}])
    assert "Started 2 worker(s) in parallel" in reply and "NO results yet" in reply
    jobs = children_jobs(env)
    assert len(jobs) == 2 and all(j["status"] == "queued" for j in jobs)
    group = get_group(jobs[0]["payload"]["group_id"], db_path=env.db)
    assert group["kind"] == "chat" and group["continuation_type"] == "agent.delegate_report"
    assert group["continuation_payload"]["chat_id"] == 4242  # the report goes to the asking chat
    assert group["continuation_payload"]["goal"] == "look around"


def test_the_tool_refuses_from_inside_a_plan(env):
    reply = _tool(env, [{"brief": "x"}], conversation_id="plan-plan.abc123")
    assert "executing inside a plan" in reply and "fanout" in reply
    assert children_jobs(env) == []


@pytest.mark.parametrize("bad", [None, [], "audit everything", [{"brief": "ok"}, "not a dict"]])
def test_the_tool_rejects_malformed_tasks(env, bad):
    assert _tool(env, bad).startswith("Error: tasks must be")
    assert children_jobs(env) == []


def test_the_tool_reports_refusals_and_starts_nothing(env):
    assert "nothing was started" in _tool(env, [{"brief": ""}])
    assert "too many children" in _tool(env, [{"brief": f"c{i}"} for i in range(7)])
    assert "unknown profile" in _tool(env, [{"brief": "ok"}, {"brief": "x", "profile": "root"}])
    assert children_jobs(env) == []


def test_chat_delegation_end_to_end_one_report_one_capture(env):
    reply = _tool(env, [{"brief": "look at A"}, {"brief": "look at B"}, {"brief": "FAIL on C"}], goal="survey")
    assert "Started 3" in reply
    with world() as w:
        drain(env)
    assert len(w.delivered) == 1 and len(w.captured) == 1
    message = w.delivered[0]
    assert message.startswith("⚠️ Delegated work finished: survey (2 of 3 succeeded)")
    assert "done: look at A" in message and "worker blew up" in message
    assert "delegation" in w.captured[0]["text"] and "worker blew up" in w.captured[0]["text"]


def test_the_tool_is_registered_and_described_honestly():
    from lisan.tools.execution_tools import TOOLS, build_tool_handlers

    spec = next(t for t in TOOLS if t["name"] == "delegate")
    assert "NOT have the results" in spec["description"] or "do NOT have the results" in spec["description"]
    handlers = build_tool_handlers(vault=__import__("pathlib").Path("/nonexistent"), db_path=None)
    assert "delegate" in handlers
