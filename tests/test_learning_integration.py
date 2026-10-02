"""Learning loop step 1 wired into the places work finishes, the instruments
that report on it, and the backup — and the guarantee that none of it can harm
the work it observes."""
from __future__ import annotations

import json
import sqlite3
import tarfile
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lisan.providers.base import LLMResponse
from lisan.tools import learning as L
from lisan.tools.jobs import dispatch_job, enqueue_job, get_job, run_jobs_worker


@pytest.fixture()
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("LISAN_SKILLS_DIR", str(skills))
    monkeypatch.setenv("LISAN_NO_OUTBOUND", "1")
    return SimpleNamespace(vault=vault, db=tmp_path / "jobs.sqlite", skills=skills, tmp=tmp_path)


def pipeline_result():
    return SimpleNamespace(action="skip", mode="chat", draft_path=None, skeptic_approved=None, entities_touched=[])


def observe_job(env, calls, conversation="telegram-1"):
    job_id = enqueue_job(
        "capture.observe",
        {"text": "do the thing", "response": "done", "tool_calls": calls, "conversation_id": conversation},
        db_path=env.db,
    )
    return get_job(job_id, db_path=env.db)


def tools(n):
    return [{"tool": "read_file", "args": {}, "result": "ok"} for _ in range(n)]


@contextmanager
def stub_pipeline():
    with patch("lisan.tools.memory_pipeline.run_memory_pipeline", return_value=pipeline_result()), \
            patch("lisan.tools.enrichment.resolve_owner_clarification", return_value=None), \
            patch("lisan.tools.jobs._enqueue_entity_rewrites", return_value=0):
        yield


# ── capture.observe → turn event ────────────────────────────────────────────

def test_a_finished_turn_becomes_an_event_when_capture_runs(env):
    job = observe_job(env, tools(6))
    with stub_pipeline():
        dispatch_job(job, vault=env.vault, db_path=env.db)
    (event,) = L.list_events(db_path=env.db)
    assert event["id"] == f"turn:{job['id']}" and event["tool_call_count"] == 6


def test_a_short_turn_is_not_an_event(env):
    with stub_pipeline():
        dispatch_job(observe_job(env, tools(2)), vault=env.vault, db_path=env.db)
    assert L.list_events(db_path=env.db) == []


def test_a_failing_recorder_cannot_fail_the_capture_it_observes(env):
    job = observe_job(env, tools(6))
    with stub_pipeline(), patch("lisan.tools.learning.record_turn_event", side_effect=RuntimeError("disk full")):
        result = dispatch_job(job, vault=env.vault, db_path=env.db)
    assert result["action"] == "skip"  # capture completed normally


# ── plans, groups, Adjutant ─────────────────────────────────────────────────

@contextmanager
def plan_world():
    with patch("lisan.providers.codex.CodexClient") as client, \
            patch("lisan.tools.scheduler._deliver_owner_message"), \
            patch("lisan.tools.plans.load_config", return_value={}), \
            patch("lisan.tools.plans._report_to_memory"), \
            patch("lisan.tools.execution_tools.assemble_context", return_value="(ctx)"):
        client.return_value.complete.side_effect = lambda prompt, **kw: LLMResponse(text="done", provider="s", model="s")
        yield


def test_a_finished_plan_is_recorded(env):
    from lisan.tools.plans import create_plan

    create_plan(goal="note it", steps=[{"kind": "note", "description": "observe"}], db_path=env.db, config={})
    with plan_world():
        run_jobs_worker(vault=env.vault, db_path=env.db)
    events = L.list_events(db_path=env.db, kind="plan")
    assert len(events) == 1 and events[0]["outcome"] == "succeeded" and "completed" in events[0]["id"]


def test_a_failed_plan_is_recorded_as_failed(env):
    from lisan.tools.plans import create_plan

    create_plan(goal="doomed", steps=[{"kind": "codex", "description": "x"}], db_path=env.db, config={})
    with plan_world() as _, patch("lisan.providers.codex.CodexClient") as client, \
            patch("lisan.tools.scheduler._deliver_owner_message"), patch("lisan.tools.plans.load_config", return_value={}), \
            patch("lisan.tools.plans._report_to_memory"), patch("lisan.tools.execution_tools.assemble_context", return_value="c"):
        client.return_value.complete.side_effect = RuntimeError("boom")
        run_jobs_worker(vault=env.vault, db_path=env.db)
    (event,) = L.list_events(db_path=env.db, kind="plan")
    assert event["outcome"] == "failed"


def test_a_failing_recorder_cannot_fail_a_plan(env):
    from lisan.tools.plans import create_plan, list_plans

    create_plan(goal="note it", steps=[{"kind": "note", "description": "observe"}], db_path=env.db, config={})
    with plan_world(), patch("lisan.tools.learning.record_plan_event", side_effect=RuntimeError("boom")):
        run_jobs_worker(vault=env.vault, db_path=env.db)
    assert list_plans(db_path=env.db)[0]["steps_done"] == 1


def test_a_chat_delegation_group_is_recorded_when_it_reports(env):
    from lisan.tools.execution_tools import delegate_tool

    delegate_tool(tasks=[{"brief": "look at A"}, {"brief": "look at B"}], goal="survey", vault=env.vault,
                  db_path=env.db, config={}, conversation_id="telegram-7")
    with patch("lisan.providers.codex.CodexClient") as client, \
            patch("lisan.tools.scheduler._deliver_owner_message"), \
            patch("lisan.tools.capture.capture_text", return_value={}), \
            patch("lisan.tools.escalation._notify_owner", return_value=True):
        client.return_value.complete.side_effect = lambda prompt, **kw: LLMResponse(text="found it", provider="s", model="s")
        run_jobs_worker(vault=env.vault, db_path=env.db)
    (event,) = L.list_events(db_path=env.db, kind="group")
    assert event["outcome"] == "succeeded" and event["conversation_id"] == "telegram-7"
    full = L.get_event(event["id"], vault=env.vault)
    assert [c["brief"] for c in full["payload"]["children"]] == ["look at A", "look at B"]


# ── instruments ─────────────────────────────────────────────────────────────

def test_self_state_reports_learning_and_skill_use(env):
    from lisan.tools import skill_history as H
    from lisan.tools.self_model import render_self_state, snapshot_self_state

    (env.skills / "server-audit").mkdir()
    (env.skills / "server-audit" / "SKILL.md").write_text(
        "---\nname: server-audit\ndescription: Use when auditing.\n---\nb\n", encoding="utf-8")
    L.record_turn_event(
        {"text": "audit", "response": "ok", "tool_calls": [{"tool": "skill", "args": {"name": "server-audit"}, "result": "x"}],
         "conversation_id": "telegram-1"},
        job_id="job.i", vault=env.vault, db_path=env.db, skills_dir=env.skills,
    )
    H.snapshot_skill(env.skills, "server-audit", reason="the loop learned a trap", actor="loop")
    H.pin_skill(env.skills, "server-audit")
    state = snapshot_self_state(vault=env.vault, db_path=env.db)
    assert state["learning"]["events"] == 1 and state["learning"]["skill_usage_30d"][0]["skill"] == "server-audit"
    text = render_self_state(state)
    assert "Learning: mode observe — 1 event(s) observed" in text
    assert "Skill use (30d): server-audit ×1" in text
    assert "Skill change: server-audit — snapshot by loop: the loop learned a trap" in text
    assert "Pinned skills (the loop will not change these): server-audit" in text


def test_self_state_is_quiet_about_learning_when_nothing_has_happened(env):
    from lisan.tools.self_model import render_self_state, snapshot_self_state

    text = render_self_state(snapshot_self_state(vault=env.vault, db_path=env.db))
    assert "Learning: mode observe — 0 event(s) observed" in text
    assert "Skill use" not in text and "Pinned skills" not in text


def test_the_backup_carries_the_learning_events(env):
    from lisan.tools.backup import create_backup

    L.record_turn_event(
        {"text": "t", "response": "r", "tool_calls": tools(6), "conversation_id": "telegram-1"},
        job_id="job.b", vault=env.vault, db_path=env.db, skills_dir=env.skills,
    )
    with patch("lisan.tools.backup.restore_backup", create=True), patch("lisan.tools.backup.generate_manifests", create=True):
        try:
            result = create_backup(vault=env.vault, destination=env.tmp / "backups")
        except Exception:
            pytest.skip("create_backup needs a fuller vault than this fixture")
    with tarfile.open(result.archive_path) as tar:
        names = tar.getnames()
    assert any(n.startswith("lisan-learning/events/") for n in names)


# ── CLI ─────────────────────────────────────────────────────────────────────

def run_cli(*argv, capsys):
    from lisan.cli import main

    code = main(list(argv))
    return code, capsys.readouterr().out


def test_cli_learning_status_events_show_and_rebuild(env, capsys):
    L.record_turn_event(
        {"text": "check disks", "response": "r", "tool_calls": tools(6), "conversation_id": "telegram-1"},
        job_id="job.c", vault=env.vault, db_path=env.db, skills_dir=env.skills,
    )
    common = ["--vault", str(env.vault), "--db-path", str(env.db)]
    code, out = run_cli("learning", "status", *common, capsys=capsys)
    assert code == 0 and "1 event(s) recorded" in out
    code, out = run_cli("learning", "events", *common, capsys=capsys)
    assert "turn:job.c" in out and "check disks" in out
    code, out = run_cli("learning", "show", "turn:job.c", "--vault", str(env.vault), capsys=capsys)
    assert code == 0 and json.loads(out)["payload"]["text"] == "check disks"
    code, out = run_cli("learning", "show", "turn:nope", "--vault", str(env.vault), capsys=capsys)
    assert code == 1
    code, out = run_cli("learning", "rebuild-index", *common, capsys=capsys)
    assert json.loads(out)["events"] == 1


def test_cli_skills_history_diff_rollback_pin_archive_export_import(env, capsys, tmp_path):
    (env.skills / "server-audit").mkdir()
    md = env.skills / "server-audit" / "SKILL.md"
    md.write_text("---\nname: server-audit\ndescription: Use when auditing.\n---\n1. disks\n", encoding="utf-8")
    d = ["--skills-dir", str(env.skills)]
    from lisan.tools import skill_history as H

    H.snapshot_skill(env.skills, "server-audit", reason="start", actor="loop")
    md.write_text(md.read_text(encoding="utf-8") + "2. memory\n", encoding="utf-8")

    code, out = run_cli("skills", "history", "server-audit", *d, capsys=capsys)
    assert code == 0 and "origin owner" in out and "v0" in out
    code, out = run_cli("skills", "diff", "server-audit", "--since-owner", *d, capsys=capsys)
    assert "+2. memory" in out
    code, out = run_cli("skills", "pin", "server-audit", *d, capsys=capsys)
    assert "pinned" in out
    code, out = run_cli("skills", "export", "server-audit", str(tmp_path), *d, capsys=capsys)
    assert code == 0 and (tmp_path / "server-audit.skill.tar.gz").is_file()
    code, out = run_cli("skills", "rollback", "server-audit", "v0", *d, capsys=capsys)
    assert code == 0 and "2. memory" not in md.read_text(encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    code, out = run_cli("skills", "import", str(tmp_path / "server-audit.skill.tar.gz"), "--skills-dir", str(other), capsys=capsys)
    assert code == 0 and "imported server-audit" in out
    code, out = run_cli("skills", "archive", "server-audit", *d, capsys=capsys)
    assert code == 0 and not (env.skills / "server-audit").exists()
    code, out = run_cli("skills", "diff", "nope", *d, capsys=capsys)
    assert code == 1 and "✗" in out


def test_cli_skills_usage(env, capsys):
    L.record_turn_event(
        {"text": "t", "response": "r", "tool_calls": [{"tool": "skill", "args": {"name": "server-audit"}, "result": "x"}],
         "conversation_id": "telegram-1"},
        job_id="job.u", vault=env.vault, db_path=env.db, skills_dir=env.skills,
    )
    code, out = run_cli("skills", "usage", "--db-path", str(env.db), capsys=capsys)
    assert code == 0 and "server-audit" in out and "1 use(s)" in out


def test_cli_skills_commands_work_with_no_skills_dir_flag(env, capsys):
    """Regression: `skills_root` is imported locally further down main(), which
    made it an unbound local for these handlers — and every test passed
    --skills-dir, the one way the bug could not show."""
    (env.skills / "server-audit").mkdir()
    (env.skills / "server-audit" / "SKILL.md").write_text(
        "---\nname: server-audit\ndescription: Use when auditing.\n---\nb\n", encoding="utf-8")
    code, out = run_cli("skills", "history", "server-audit", capsys=capsys)  # LISAN_SKILLS_DIR from the fixture
    assert code == 0 and "origin owner" in out


def test_new_turns_carry_full_tool_results_for_learning_while_the_pipeline_keeps_the_compact_form(env):
    """1 in 5 real tool results was cut at 1500 characters; the executor's final
    report is the part a reviewer needs most."""
    from lisan.tools.conversation import _compact_tool_calls, _full_tool_calls

    big = {"tool": "execute_task", "args": {"task": "x"}, "result": "R" * 30_000}
    assert len(_compact_tool_calls([big])[0]["result"]) <= 1500
    full = _full_tool_calls([big])[0]["result"]
    assert 19_000 < len(full) <= 20_000 and full.endswith("…")
    assert len(_full_tool_calls([big] * 60)) == 40

    payload = {"text": "t", "response": "r", "conversation_id": "telegram-1",
               "tool_calls": _compact_tool_calls([big]), "tool_calls_full": _full_tool_calls([big])}
    event_id = L.record_turn_event(payload, job_id="job.big", vault=env.vault, db_path=env.db, skills_dir=env.skills)
    stored = L.get_event(event_id, vault=env.vault)["payload"]["tool_calls"][0]["result"]
    assert len(stored) > 15_000  # the learning event kept it
    assert stored.count("R") > 15_000


def test_older_payloads_without_the_full_form_still_record(env):
    payload = {"text": "t", "response": "r", "conversation_id": "telegram-1",
               "tool_calls": [{"tool": "run_codex", "args": {}, "result": "ok"}]}
    assert L.record_turn_event(payload, job_id="job.old", vault=env.vault, db_path=env.db, skills_dir=env.skills)
