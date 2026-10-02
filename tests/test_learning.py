"""Learning loop step 1: observe (docs/learning_loop_workorder.md).

Claims: events are frozen, idempotent and append-only plain files with a
rebuildable index; provenance is recorded but never a gate; eval history is
never recorded; the skill ledger is derived from what a turn actually did; and
recording can never harm the work it observes.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lisan.tools import learning as L


@pytest.fixture()
def env(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    skills = tmp_path / "skills"
    (skills / "server-audit").mkdir(parents=True)
    (skills / "server-audit" / "SKILL.md").write_text(
        "---\nname: server-audit\ndescription: Use when auditing a server.\nversion: 1.2.0\n---\nsteps\n", encoding="utf-8"
    )
    (skills / "gmail_read").mkdir()
    (skills / "gmail_read" / "SKILL.md").write_text(
        "---\nname: gmail_read\ndescription: Use when reading mail.\n---\nx\n", encoding="utf-8"
    )
    (skills / "gmail_read" / "schema.json").write_text('{"name": "gmail_read", "description": "d", "parameters": {}}', encoding="utf-8")
    (skills / "gmail_read" / "tool.py").write_text("def run(args, vault, config):\n    return ''\n", encoding="utf-8")
    return SimpleNamespace(vault=vault, db=tmp_path / "learn.sqlite", skills=skills, tmp=tmp_path)


def call(tool, args=None, result="ok"):
    return {"tool": tool, "args": args or {}, "result": result}


def turn(env, calls, *, job="job.1", conversation="telegram-1", text="please do the thing", config=None, **kw):
    payload = {"text": text, "response": "done", "tool_calls": calls, "conversation_id": conversation}
    return L.record_turn_event(
        payload, job_id=job, vault=env.vault, db_path=env.db, config=config, skills_dir=env.skills, **kw
    )


def many(n, tool="read_file"):
    return [call(tool) for _ in range(n)]


# ── configuration ───────────────────────────────────────────────────────────

def test_mode_defaults_to_observe_and_unknown_values_do_not_switch_it_off():
    assert L.learning_mode(None) == L.learning_mode({}) == "observe"
    assert L.learning_mode({"learning": {"mode": "AUTO"}}) == "auto"
    assert L.learning_mode({"learning": {"mode": "autoo"}}) == "observe"


def test_off_records_nothing(env):
    assert turn(env, many(9), config={"learning": {"mode": "off"}}) is None
    assert L.list_events(db_path=env.db) == [] and not (env.tmp / "learning").exists()


# ── which turns become events ───────────────────────────────────────────────

def test_a_turn_is_recorded_at_the_tool_call_threshold(env):
    assert turn(env, many(4), job="job.a") is None  # below 5, no skill
    assert turn(env, many(5), job="job.b") == "turn:job.b"
    assert turn(env, many(2), job="job.c", config={"learning": {"min_tool_calls": 2}}) == "turn:job.c"


def test_a_short_turn_that_used_a_skill_is_recorded(env):
    event_id = turn(env, [call("skill", {"name": "server-audit"})], job="job.s")
    assert event_id == "turn:job.s"
    assert L.get_event(event_id, vault=env.vault)["skills_used"] == [{"skill": "server-audit", "kind": "instructional"}]


def test_eval_and_rehearsal_conversations_are_never_recorded(env):
    for conversation in ("eval-run-3", "scale-0001", "cap-x", "grow-9", "hermes-test", "baseline-1", "wo5-a"):
        assert turn(env, many(9), job=f"job.{conversation}", conversation=conversation) is None
    assert L.list_events(db_path=env.db) == []


def test_a_turn_inside_a_plan_is_not_an_event_but_its_skill_use_is_counted(env):
    assert turn(env, [call("skill", {"name": "server-audit"})] + many(8), job="job.p", conversation="plan-plan.abc") is None
    assert L.list_events(db_path=env.db) == []
    usage = L.skill_usage_summary(env.db)
    assert usage and usage[0]["skill"] == "server-audit" and usage[0]["uses"] == 1


def test_recording_is_idempotent_by_job(env):
    assert turn(env, many(5), job="job.same") == "turn:job.same"
    assert turn(env, many(5), job="job.same") is None  # a retried job records nothing twice
    assert len(list(L.iter_events(env.vault))) == 1


# ── what is frozen ──────────────────────────────────────────────────────────

def test_the_event_freezes_what_a_reviewer_will_need(env):
    calls = [call("read_file", {"path": "/etc/hosts"}, "127.0.0.1 localhost")] + many(4)
    event_id = turn(env, calls, job="job.f", text="check the hosts file")
    event = L.get_event(event_id, vault=env.vault)
    assert event["kind"] == "turn" and event["conversation_id"] == "telegram-1"
    assert event["payload"]["text"] == "check the hosts file" and event["payload"]["response"] == "done"
    assert event["payload"]["tool_calls"][0]["result"] == "127.0.0.1 localhost"
    assert event["tool_call_count"] == 5 and event["tools"] == ["read_file"]
    assert event["refs"] == {"job_id": "job.f"} and event["occurred_at"] and event["recorded_at"]


def test_a_tool_error_is_reported_as_such_and_its_absence_is_not_called_success(env):
    ok = turn(env, many(5), job="job.ok")
    bad = turn(env, many(4) + [call("execute_task", result="Error: exit code 2")], job="job.bad")
    assert L.get_event(ok, vault=env.vault)["outcome"] == "no_error_seen"  # honest: not "succeeded"
    assert L.get_event(bad, vault=env.vault)["outcome"] == "tool_error"


# ── provenance: recorded, never a gate ──────────────────────────────────────

@pytest.mark.parametrize("calls,sources,tainted", [
    (many(5), ["owner"], False),
    (many(4) + [call("gmail_search")], ["email", "owner"], True),
    (many(4) + [call("browser")], ["owner", "web"], True),
    (many(4) + [call("execute_task", {"task": "curl https://example.com/x"})], ["owner", "web"], True),
    (many(4) + [call("execute_task", {"task": "ssh root@db01 uptime"})], ["owner", "remote_host"], True),
    (many(4) + [call("execute_task", {"task": "ls -la ~/Documents"}, "total 8")], ["owner"], False),
    (many(4) + [call("execute_task", {"task": "build it"}, "fetched http://x.test/a")], ["owner", "web"], True),
])
def test_sources_and_taint_come_from_tool_names_and_visible_activity(env, calls, sources, tainted):
    event = L.get_event(turn(env, calls, job="job.t"), vault=env.vault)
    assert event["sources"] == sources and event["tainted"] is tainted


def test_tainted_events_are_recorded_like_any_other(env):
    event_id = turn(env, many(4) + [call("gmail_read")], job="job.x")
    assert event_id and L.list_events(db_path=env.db)[0]["tainted"] == 1  # provenance, not a gate


# ── the skill ledger ────────────────────────────────────────────────────────

def test_skill_use_is_derived_for_instructional_and_executable_skills(env):
    known = L.known_skills(env.skills)
    assert known == {"server-audit": "instructional", "gmail_read": "executable"}
    calls = [call("skill", {"name": "server-audit"}), call("gmail_read"), call("skill", {"name": "server-audit"}), call("read_file")]
    assert L.skills_used(calls, known) == [
        {"skill": "server-audit", "kind": "instructional"},
        {"skill": "gmail_read", "kind": "executable", "outcome": "no_error_seen"},
    ]


def test_an_executable_skills_outcome_is_judged_from_its_own_calls_not_the_turn(env):
    """Measured on real history: an unrelated tool erroring in the same turn used
    to be charged to the skill."""
    known = L.known_skills(env.skills)
    unrelated = [call("gmail_read", result="3 messages"), call("execute_task", result="Error: exit code 1")]
    assert L.skills_used(unrelated, known)[0]["outcome"] == "no_error_seen"
    its_own = [call("gmail_read", result="Error: token expired"), call("read_file")]
    assert L.skills_used(its_own, known)[0]["outcome"] == "tool_error"
    event_id = turn(env, unrelated + many(4), job="job.o")
    assert L.get_event(event_id, vault=env.vault)["outcome"] == "tool_error"  # the turn did hit an error...
    assert L.skill_usage_summary(env.db)[0]["outcomes"] == {"no_error_seen": 1}  # ...but not the skill


def test_an_instructional_skill_inherits_the_turns_outcome(env):
    turn(env, [call("skill", {"name": "server-audit"}), call("execute_task", result="Error: boom")], job="job.i")
    assert L.skill_usage_summary(env.db)[0]["outcomes"] == {"tool_error": 1}


def test_handing_work_to_the_executor_is_recorded_whatever_the_call_count(env):
    """Measured: 65 of 583 real turns handed work to run_codex, but only 5 made 5+ calls."""
    assert turn(env, [call("run_codex", {"task": "rotate the logs"})], job="job.h1") == "turn:job.h1"
    assert turn(env, [call("execute_task", {"task": "fix it"})], job="job.h2") == "turn:job.h2"
    assert turn(env, [call("checkin"), call("self_state")], job="job.h3") is None  # not work for the executor
    assert turn(env, [call("run_codex")], job="job.h4", config={"learning": {"work_tools": []}}) is None  # configurable
    assert turn(env, [call("schedule_task")], job="job.h5", config={"learning": {"work_tools": ["schedule_task"]}}) == "turn:job.h5"


def test_usage_rows_carry_the_skill_version_and_the_events_outcome(env):
    turn(env, [call("skill", {"name": "server-audit"})], job="job.u1")
    turn(env, [call("skill", {"name": "server-audit"}), call("execute_task", result="Error: boom")], job="job.u2")
    (row,) = L.skill_usage_summary(env.db)
    assert row["skill"] == "server-audit" and row["uses"] == 2
    assert row["outcomes"] == {"no_error_seen": 1, "tool_error": 1}
    conn = sqlite3.connect(env.db)
    assert {r[0] for r in conn.execute("SELECT version FROM skill_usage")} == {"1.2.0"}


def test_the_usage_window_excludes_old_uses(env):
    turn(env, [call("skill", {"name": "server-audit"})], job="job.old", occurred_at="2020-01-01T00:00:00Z")
    assert L.skill_usage_summary(env.db, days=30) == []
    assert L.skill_usage_summary(env.db, days=None)[0]["uses"] == 1


# ── plans, groups, Adjutant ─────────────────────────────────────────────────

def _plan(**kw):
    return {"plan_id": "plan.abc", "goal": "count the tests", "resume_count": 0, "conversation_id": "telegram-1",
            "steps": [{"kind": "codex", "description": "count", "status": "done", "attempts": 1, "result": "49"}], **kw}


def test_a_plan_is_recorded_with_its_outcome_and_resume_count(env):
    done = L.record_plan_event(_plan(), "completed", vault=env.vault, db_path=env.db)
    failed = L.record_plan_event(_plan(resume_count=1), "failed", vault=env.vault, db_path=env.db)
    canceled = L.record_plan_event(_plan(resume_count=2), "canceled", vault=env.vault, db_path=env.db)
    assert (done, failed, canceled) == ("plan:plan.abc:r0:completed", "plan:plan.abc:r1:failed", "plan:plan.abc:r2:canceled")
    outcomes = {e["id"]: e["outcome"] for e in L.iter_events(env.vault)}
    assert outcomes[done] == "succeeded" and outcomes[failed] == "failed" and outcomes[canceled] == "canceled"


def test_a_fanout_steps_children_are_part_of_the_plan_event(env):
    plan = _plan()
    plan["steps"].append({"kind": "fanout", "description": "split", "status": "done", "children": [{"brief": "look at A"}], "join": "all"})
    event = L.get_event(L.record_plan_event(plan, "completed", vault=env.vault, db_path=env.db), vault=env.vault)
    assert event["payload"]["steps"][1]["children"] == ["look at A"] and event["payload"]["steps"][1]["join"] == "all"


def test_plan_provenance_notices_network_use_in_results(env):
    plan = _plan()
    plan["steps"][0]["result"] = "downloaded https://example.com/report.pdf"
    event = L.get_event(L.record_plan_event(plan, "completed", vault=env.vault, db_path=env.db), vault=env.vault)
    assert event["tainted"] is True and "web" in event["sources"]


def test_eval_plans_are_not_recorded(env):
    assert L.record_plan_event(_plan(conversation_id="eval-plan-1"), "completed", vault=env.vault, db_path=env.db) is None


def test_a_delegation_group_is_recorded_with_each_childs_outcome(env):
    children = [
        {"status": "succeeded", "brief": "audit A", "profile": "read_only", "text": "fine", "duration_s": 3},
        {"status": "failed", "brief": "audit B", "profile": "read_only", "error": "timed out"},
    ]
    event = L.get_event(
        L.record_group_event("grp.chat.1", "audit", children, conversation_id="telegram-1", vault=env.vault, db_path=env.db),
        vault=env.vault,
    )
    assert event["outcome"] == "failed" and event["kind"] == "group"
    assert [c["status"] for c in event["payload"]["children"]] == ["succeeded", "failed"]
    assert event["payload"]["children"][0]["brief"] == "audit A"  # the brief is kept for the learning loop


def test_an_adjutant_attempt_is_recorded_and_research_counts_as_web(env):
    event = L.get_event(
        L.record_adjutant_event(task_id="loop-1", attempt=2, kinds=["research"], summary="look it up", ok=True,
                                actions=["searched"], errors=[], vault=env.vault, db_path=env.db),
        vault=env.vault,
    )
    assert event["id"] == "adjutant:loop-1:a2" and event["outcome"] == "succeeded"
    assert event["sources"] == ["adjutant", "web"] and event["tainted"] is True


# ── storage: plain files are the truth ──────────────────────────────────────

def test_events_are_appended_to_a_monthly_file_beside_the_vault_not_inside_it(env):
    turn(env, many(5), job="job.m", occurred_at="2026-03-14T10:00:00Z")
    path = env.tmp / "learning" / "events" / "2026-03" / "events.jsonl"
    assert path.is_file() and json.loads(path.read_text().splitlines()[0])["id"] == "turn:job.m"
    assert not list(env.vault.rglob("*.jsonl"))  # never in the vault, so never retrievable


def test_concurrent_writers_never_corrupt_the_file(env):
    def worker(i):
        for j in range(15):
            turn(env, many(5), job=f"job.{i}.{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = (env.tmp / "learning" / "events").rglob("events.jsonl")
    raw = [line for f in lines for line in f.read_text().splitlines()]
    assert len(raw) == 90 and all(json.loads(line) for line in raw)


def test_a_torn_last_line_and_duplicate_lines_are_tolerated_on_read(env):
    turn(env, many(5), job="job.k")
    path = next((env.tmp / "learning" / "events").rglob("events.jsonl"))
    first = path.read_text().splitlines()[0]
    with path.open("a") as f:
        f.write(first + "\n")  # a duplicate, as after a crash between file write and index row
        f.write('{"id": "turn:torn", "kind": "tu')  # a torn final line
    events = list(L.iter_events(env.vault))
    assert [e["id"] for e in events] == ["turn:job.k"]


def test_the_index_and_ledger_rebuild_from_the_files(env):
    turn(env, [call("skill", {"name": "server-audit"})] + many(5), job="job.r1")
    turn(env, many(5), job="job.r2")
    conn = sqlite3.connect(env.db)
    conn.execute("DELETE FROM learning_events")
    conn.execute("DELETE FROM skill_usage")
    conn.commit()
    conn.close()
    assert L.list_events(db_path=env.db) == []
    assert L.rebuild_index(env.vault, env.db, env.skills) == {"events": 2, "usage_rows": 1}
    assert {e["id"] for e in L.list_events(db_path=env.db)} == {"turn:job.r1", "turn:job.r2"}
    assert L.skill_usage_summary(env.db)[0]["skill"] == "server-audit"


def test_compressed_months_are_still_read(env):
    turn(env, many(5), job="job.gz")
    path = next((env.tmp / "learning" / "events").rglob("events.jsonl"))
    gz = path.with_suffix(".jsonl.gz")
    gz.write_bytes(gzip.compress(path.read_bytes()))
    path.unlink()  # compress, never delete: the data is still there
    assert [e["id"] for e in L.iter_events(env.vault)] == ["turn:job.gz"]
    assert L.rebuild_index(env.vault, env.db, env.skills)["events"] == 1


def test_status_reports_mode_counts_provenance_and_storage(env):
    turn(env, many(5), job="job.s1")
    turn(env, many(4) + [call("browser")], job="job.s2")
    status = L.learning_status(env.vault, env.db, None)
    assert status["mode"] == "observe" and status["events"] == 2 and status["tainted"] == 1
    assert status["by_kind"] == {"turn": 2} and status["bytes"] > 0
    text = L.format_status(status)
    assert "2 event(s) recorded" in text and "provenance, not a gate" in text


# ── backfill from history ───────────────────────────────────────────────────

def _history(env):
    from lisan.tools.jobs import enqueue_job, mark_job_succeeded

    def observe(calls, conversation="telegram-1"):
        job_id = enqueue_job("capture.observe", {"text": "t", "response": "r", "tool_calls": calls,
                                                  "conversation_id": conversation}, db_path=env.db)
        mark_job_succeeded(job_id, result={}, db_path=env.db)
        return job_id

    return observe(many(6)), observe(many(2)), observe(many(8), conversation="eval-x")


def test_backfill_applies_the_live_rules_to_history_and_is_idempotent(env):
    kept, short, evalrun = _history(env)
    first = L.backfill(env.vault, env.db, None, skills_dir=env.skills)
    assert first["turns_seen"] == 3 and first["turn_events"] == 1  # short and eval turns are not events
    assert {e["id"] for e in L.iter_events(env.vault)} == {f"turn:{kept}"}
    assert L.backfill(env.vault, env.db, None, skills_dir=env.skills)["turn_events"] == 0


def test_backfill_since_is_a_lower_bound(env):
    _history(env)
    assert L.backfill(env.vault, env.db, None, since="2999-01-01", skills_dir=env.skills)["turns_seen"] == 0


# ── recording can never harm the work it observes ───────────────────────────

def test_a_turn_with_no_tool_calls_or_malformed_ones_is_harmless(env):
    assert turn(env, []) is None
    assert L.record_turn_event({"text": "x", "tool_calls": "garbage"}, job_id="j", vault=env.vault, db_path=env.db) is None
    assert L.record_turn_event({"text": "x", "tool_calls": [None, 3, {}]}, job_id="j2", vault=env.vault, db_path=env.db) is None
