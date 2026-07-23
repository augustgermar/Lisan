"""WO-ADJUTANT step 4: executor, reporter, confirmations.

Binding claims: scripts run only from the allowlist with args from the
record; every result — success or failure — goes out through the capture
front door and nowhere else; two failures block a task; confirmations
are records first (mirror synced via reindex), deduped, expiring, and an
owner's approval executes on the next cycle — unless a never-rule now
says otherwise.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_confirmations import (
    approve_confirmation,
    create_confirmation_for_task,
    deny_confirmation,
    expire_stale_confirmations,
    list_pending,
)
from lisan.tools.adjutant_executor import execute_collect, execute_draft, execute_run_script
from lisan.tools.adjutant_reporter import render_result_turn, report_result
from lisan.tools.adjutant_runner import run_cycle
from lisan.tools.db import connect as db_connect
from lisan.tools.intent import _record_known_hash, init_intent, intent_path
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.record_factory import new_open_loop, new_schedule

DELEGATIONS = {
    "defaults": {"mode": "report_only"},
    "arenas": {
        "work": {
            "mode": "execute",
            "capabilities": ["run_local_scripts", "read_files", "write_files"],
            "confirm_required": ["git_push"],
        },
    },
    "global": {
        "send_outbound_message": "confirm_always",
        "max_task_wall_seconds": 30,
        "max_tasks_per_cycle": 5,
    },
}


@pytest.fixture()
def world(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    init_intent(vault)
    path = intent_path(vault)
    doc = load_markdown(path)
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(DELEGATIONS, indent=2) + "\n```" + body[end:]
    fm = dict(doc.frontmatter)
    # Adopt the document: clear the template sentinels, or enabled cycles halt.
    fm.update(created="2026-07-23", updated="2026-07-23", review_after="2026-10-23")
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    _record_known_hash(vault)

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    db = tmp_path / "adjutant.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    config = {
        "adjutant": {"enabled": True, "script_dirs": [str(scripts)], "collect_paths": []},
    }
    return vault, db, conn, config, scripts, tmp_path


def _script(scripts_dir, name, body="#!/bin/sh\necho hello from $0\n", mode=0o755):
    path = scripts_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def _task_loop(vault, conn, title, *, kind="run_script", payload=None, arena="work"):
    created = new_open_loop(vault, title, domain_primary=arena)
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(task_kind=kind, task_payload=payload or {}, task_status="pending", execute_asap=True)
    write_markdown(created.path, fm, doc.body)
    assert index_single_record(created.path, vault, conn)
    conn.commit()
    return fm["id"], created.path


class CaptureSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return {"captured": True}


# ---------------------------------------------------------------------------
# Executor units

def test_run_script_allowlisted_captures_output(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "greet.sh", "#!/bin/sh\necho hello adjutant\necho warn >&2\nexit 0\n")
    result = execute_run_script("t1", {"script": "greet.sh"}, config=config, timeout_seconds=10, scratch_root=tmp)
    assert result.ok and result.exit_code == 0
    assert "hello adjutant" in result.stdout
    assert "warn" in result.stderr


def test_run_script_outside_allowlist_refuses(world):
    vault, db, conn, config, scripts, tmp = world
    rogue = tmp / "rogue.sh"
    rogue.write_text("#!/bin/sh\necho pwned\n")
    rogue.chmod(0o755)
    for name in ["../rogue.sh", str(rogue), "missing.sh"]:
        result = execute_run_script("t1", {"script": name}, config=config, timeout_seconds=10, scratch_root=tmp)
        assert not result.ok
        assert result.exit_code is None  # never started
    result = execute_run_script("t1", {"script": "x.sh", "args": [{"cmd": "boom"}]}, config=config, timeout_seconds=10)
    assert not result.ok and "scalars" in result.errors[0]


def test_run_script_timeout_kills(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "slow.sh", "#!/bin/sh\nsleep 30\n")
    result = execute_run_script("t1", {"script": "slow.sh"}, config=config, timeout_seconds=1, scratch_root=tmp)
    assert not result.ok
    assert any("max_task_wall_seconds" in e for e in result.errors)


def test_run_script_collects_scratch_artifacts(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "produce.sh", "#!/bin/sh\necho data > out.txt\n")
    result = execute_run_script("t1", {"script": "produce.sh"}, config=config, timeout_seconds=10, scratch_root=tmp)
    assert result.ok
    assert len(result.artifacts) == 1 and result.artifacts[0].endswith("out.txt")


def test_collect_respects_allowlist(world, tmp_path):
    vault, db, conn, config, scripts, tmp = world
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "a.pdf").write_text("x")
    (docs / "sub" / "b.pdf").write_text("y")
    (docs / "c.txt").write_text("z")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "d.pdf").write_text("w")
    config["adjutant"]["collect_paths"] = [str(docs)]

    result = execute_collect("t1", {"pattern": "*.pdf"}, config=config)
    assert result.ok
    assert {f["path"].split("/")[-1] for f in result.findings} == {"a.pdf", "b.pdf"}

    result = execute_collect("t1", {"pattern": "*.pdf", "paths": [str(outside)]}, config=config)
    assert result.findings == []
    assert any("outside adjutant.collect_paths" in e for e in result.errors)

    result = execute_collect("t1", {}, config={"adjutant": {"collect_paths": []}})
    assert not result.ok and "collect_paths is empty" in result.errors[0]


def test_draft_writes_to_drafts_via_injected_provider(world):
    vault, db, conn, config, scripts, tmp = world
    result = execute_draft(
        "t1", {"title": "Backup briefing", "instructions": "Summarize the backup posture."},
        vault=vault, complete=lambda prompt: "# Briefing\n\nAll green.",
    )
    assert result.ok and len(result.artifacts) == 1
    draft = load_markdown(vault / "drafts" / result.artifacts[0].split("/")[-1])
    assert draft.frontmatter["source"] == "adjutant"
    assert "All green" in draft.body

    result = execute_draft("t1", {"title": "x", "instructions": "y"}, vault=vault, complete=None)
    assert not result.ok and "no generation provider" in result.errors[0]
    result = execute_draft("t1", {"title": "x"}, vault=vault, complete=lambda p: "text")
    assert not result.ok and "no instructions" in result.errors[0]


# ---------------------------------------------------------------------------
# Reporter

def test_reporter_speaks_only_through_capture(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "fail.sh", "#!/bin/sh\necho broken >&2\nexit 3\n")
    result = execute_run_script("open_loop.x", {"script": "fail.sh"}, config=config, timeout_seconds=10, scratch_root=tmp)
    spy = CaptureSpy()
    report_result(vault, result, verdict_path="test-rule", capture=spy)
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["conversation_id"] == "adjutant" and call["speaker"] == "ADJUTANT"
    text = call["text"]
    assert "FAILURE" in text and "script exited 3" in text and "broken" in text
    assert "Authorized by: test-rule" in text


def test_render_reports_failure_unsoftened(world):
    vault, db, conn, config, scripts, tmp = world
    result = execute_run_script("t", {"script": "ghost.sh"}, config=config, timeout_seconds=5)
    text = render_result_turn(result)
    assert "FAILURE" in text
    assert "not under any allowlisted" in text


# ---------------------------------------------------------------------------
# Confirmations

def test_confirmation_dedupe_and_resolution(world):
    vault, db, conn, config, scripts, tmp = world
    loop_id, _ = _task_loop(vault, conn, "Push the release")
    spy = CaptureSpy()
    first = create_confirmation_for_task(
        vault, task_id=loop_id, task_summary="push", planned_action="git push", risk="public", db_path=db
    )
    assert first
    assert create_confirmation_for_task(
        vault, task_id=loop_id, task_summary="push", planned_action="git push", risk="public", db_path=db
    ) is None  # deduped
    assert [p["id"] for p in list_pending(db)] == [first]

    outcome = deny_confirmation(vault, first, db_path=db, capture=spy)
    assert outcome["resolution"] == "denied"
    assert list_pending(db) == []
    assert any("denied" in c["text"] for c in spy.calls)  # the no is memory too
    with pytest.raises(ValueError):
        deny_confirmation(vault, first, db_path=db, capture=spy)


def test_confirmation_expiry_blocks_task(world):
    vault, db, conn, config, scripts, tmp = world
    loop_id, loop_path = _task_loop(vault, conn, "Send the report")
    created = create_confirmation_for_task(
        vault, task_id=loop_id, task_summary="send", planned_action="send it", risk="outbound", db_path=db
    )
    expired = expire_stale_confirmations(vault, db, today="2030-01-01")
    assert [e["id"] for e in expired] == [created]
    assert list_pending(db) == []
    fm = load_markdown(loop_path).frontmatter
    assert fm["task_status"] == "expired"
    # Batch review surfaces both queues.
    from lisan.tools.batch_review import generate_batch_review

    digest = generate_batch_review(vault, db)
    assert "Blocked Tasks" in digest and loop_id in digest


# ---------------------------------------------------------------------------
# The enabled cycle, end to end (fake provider, spy capture)

def test_enabled_cycle_executes_and_reports(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "echo.sh", "#!/bin/sh\necho did the thing\n")
    loop_id, loop_path = _task_loop(vault, conn, "Run the echo", payload={"script": "echo.sh"})
    conn.close()
    spy = CaptureSpy()
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert not result["halted"] and result["dry_run"] is False
    assert len(result["executed"]) == 1 and result["executed"][0]["ok"]
    assert load_markdown(loop_path).frontmatter["task_status"] == "resolved"
    assert any("did the thing" in c["text"] for c in spy.calls)
    check = db_connect(db)
    run_row = check.execute("SELECT * FROM task_runs WHERE task_id=?", (loop_id,)).fetchone()
    assert run_row is not None and run_row[4] is not None  # finished
    check.close()
    # Resolved task is not re-selected next cycle.
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert result["executed"] == []


def test_two_failures_block_the_task(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "boom.sh", "#!/bin/sh\nexit 9\n")
    loop_id, loop_path = _task_loop(vault, conn, "Run the boom", payload={"script": "boom.sh"})
    conn.close()
    spy = CaptureSpy()
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert not result["executed"][0]["ok"]
    assert load_markdown(loop_path).frontmatter["task_status"] == "pending"  # one strike
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert load_markdown(loop_path).frontmatter["task_status"] == "blocked"  # two strikes
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert result["executed"] == []  # blocked tasks are not re-polled
    # Both failures were reported through capture, unsoftened.
    failures = [c for c in spy.calls if "FAILURE" in c["text"]]
    assert len(failures) == 2


def test_confirm_verdict_creates_confirmation_then_approval_executes(world):
    vault, db, conn, config, scripts, tmp = world
    # git_push is confirm_required in work: use a draft task in an arena
    # where drafting demands confirmation instead — simpler: notify kind is
    # globally confirm_always but lands in step 5; use git_push mapping via
    # a run_script task in a report_only arena? No — use kind draft in an
    # unlisted arena = report_only, not confirm. So: extend the loop with a
    # payload and rely on global confirm for send? Cleanest real case:
    # a task whose kind maps to a confirm_required capability. None of the
    # local kinds map to git_push, so wire an explicit confirm: run_script
    # in work arena with global run_local_scripts=confirm_always.
    config2 = dict(config)
    path = intent_path(vault)
    doc = load_markdown(path)
    delegations = dict(DELEGATIONS)
    delegations["global"] = dict(delegations["global"])
    delegations["global"]["run_local_scripts"] = "confirm_always"
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
    write_markdown(path, dict(doc.frontmatter), body)
    _record_known_hash(vault)

    _script(scripts, "deploy.sh", "#!/bin/sh\necho deployed\n")
    loop_id, loop_path = _task_loop(vault, conn, "Deploy the thing", payload={"script": "deploy.sh"})
    conn.close()
    spy = CaptureSpy()

    result = run_cycle(vault, db, config=config2, capture=spy, scratch_root=tmp)
    assert result["verdicts"][0]["verdict"] == "confirm"
    pending = list_pending(db)
    assert len(pending) == 1
    # Second cycle: deduped, still exactly one.
    run_cycle(vault, db, config=config2, capture=spy, scratch_root=tmp)
    assert len(list_pending(db)) == 1

    approve_confirmation(vault, pending[0]["id"], db_path=db, capture=spy)
    result = run_cycle(vault, db, config=config2, capture=spy, scratch_root=tmp)
    executed = result["executed"]
    assert len(executed) == 1 and executed[0]["ok"] and executed[0]["task_id"] == loop_id
    assert any("deployed" in c["text"] for c in spy.calls)
    assert load_markdown(loop_path).frontmatter["task_status"] == "resolved"
    # The approved confirmation closed after execution.
    check = db_connect(db)
    row = check.execute("SELECT status FROM confirmations WHERE task_id=?", (loop_id,)).fetchone()
    assert row[0] == "resolved"
    check.close()


def test_never_rule_overrides_stale_approval(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "deploy.sh", "#!/bin/sh\necho deployed\n")
    loop_id, loop_path = _task_loop(vault, conn, "Deploy under embargo", payload={"script": "deploy.sh"})
    created = create_confirmation_for_task(
        vault, task_id=loop_id, task_summary="deploy", planned_action="run deploy.sh", risk="x", db_path=db
    )
    spy = CaptureSpy()
    approve_confirmation(vault, created, db_path=db, capture=spy)
    # Owner then adds a never-rule before the next cycle.
    path = intent_path(vault)
    doc = load_markdown(path)
    delegations = json.loads(json.dumps(DELEGATIONS))
    delegations["global"]["run_local_scripts"] = "never"
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
    write_markdown(path, dict(doc.frontmatter), body)
    _record_known_hash(vault)
    conn.close()

    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert result["executed"] == []
    assert not any("deployed" in c["text"] for c in spy.calls)
    check = db_connect(db)
    row = check.execute(
        "SELECT note FROM adjutant_log WHERE verdict='approval_overridden'"
    ).fetchone()
    assert row is not None and loop_id in row[0]
    check.close()


def test_decision_steps_execute_in_order_and_halt_on_failure(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "ok.sh", "#!/bin/sh\necho fine\n")
    _script(scripts, "bad.sh", "#!/bin/sh\nexit 1\n")
    from lisan.tools.record_factory import new_decision

    created = new_decision(vault, "Two step plan", domain_primary="work")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["execution_steps"] = [
        {"step": "first", "task_kind": "run_script", "task_payload": {"script": "ok.sh"}, "status": "pending"},
        {"step": "second", "task_kind": "run_script", "task_payload": {"script": "bad.sh"}, "status": "pending"},
        {"step": "third", "task_kind": "run_script", "task_payload": {"script": "ok.sh"}, "status": "pending"},
    ]
    write_markdown(created.path, fm, doc.body)
    index_single_record(created.path, vault, conn)
    conn.commit()
    conn.close()
    spy = CaptureSpy()
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    steps = load_markdown(created.path).frontmatter["execution_steps"]
    assert steps[0]["status"] == "resolved"
    assert steps[1]["status"] == "pending"  # failed, retryable
    assert steps[2]["status"] == "pending"  # never reached
    assert not result["executed"][0]["ok"]


def test_schedule_advances_or_parks_after_run(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "tick.sh", "#!/bin/sh\necho tick\n")
    daily = new_schedule(
        vault, "daily tick", task_kind="run_script", cron="daily@08:00",
        next_run="2020-01-01T08:00:00", payload={"script": "tick.sh"}, domain_primary="work",
    )
    weekly = new_schedule(
        vault, "weekly tick", task_kind="run_script", cron="weekly:mon@08:00",
        next_run="2020-01-01T08:00:00", payload={"script": "tick.sh"}, domain_primary="work",
    )
    for record in (daily, weekly):
        index_single_record(record.path, vault, conn)
    conn.commit()
    conn.close()
    spy = CaptureSpy()
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert len(result["executed"]) == 2 and all(o["ok"] for o in result["executed"])
    daily_next = load_markdown(daily.path).frontmatter["next_run"]
    assert daily_next > "2026-01-01"  # advanced into the future
    assert load_markdown(weekly.path).frontmatter["next_run"] == ""  # parked until step 6
    check = db_connect(db)
    assert check.execute("SELECT 1 FROM adjutant_log WHERE verdict='schedule_parked'").fetchone()
    check.close()


def test_sentinel_dates_refuse_enabled_cycles(world, tmp_path):
    """Uncustomized authority is no authority: the seed template's 1970
    sentinels halt enabled cycles loudly; dry-run proceeds."""
    from lisan.paths import ensure_vault_layout as _evl

    vault2 = tmp_path / "vault2"
    _evl(vault2)
    init_intent(vault2)  # sentinel template, valid but unadopted
    db2 = tmp_path / "db2.sqlite"
    spy = CaptureSpy()
    result = run_cycle(vault2, db2, config={"adjutant": {"enabled": True}}, capture=spy)
    assert result["halted"] and "sentinel" in result["reason"]
    result = run_cycle(vault2, db2, config={"adjutant": {"enabled": False}}, capture=spy)
    assert not result["halted"]  # dry-run acts on nothing, so it may run


def test_has_sentinel_dates_predicate():
    from lisan.tools.intent import default_intent_document, has_sentinel_dates, parse_intent

    assert has_sentinel_dates(parse_intent(default_intent_document()))
    assert not has_sentinel_dates(parse_intent(default_intent_document(today="2026-07-23")))


def test_double_expiry_escalates_in_batch_review(world):
    vault, db, conn, config, scripts, tmp = world
    loop_id, loop_path = _task_loop(vault, conn, "Send the quarterly letter")
    for round_ in range(2):
        created = create_confirmation_for_task(
            vault, task_id=loop_id, task_summary="send", planned_action="send it", risk="outbound", db_path=db
        )
        assert created
        expire_stale_confirmations(vault, db, today="2030-01-01")
        # Re-arm the task so a second confirmation can exist.
        doc = load_markdown(loop_path)
        fm = dict(doc.frontmatter)
        fm["task_status"] = "pending"
        write_markdown(loop_path, fm, doc.body)
        index_single_record(loop_path, vault, conn)
        conn.commit()
    from lisan.tools.batch_review import generate_batch_review

    # Park it as expired for the digest (the second expiry left it pending).
    doc = load_markdown(loop_path)
    fm = dict(doc.frontmatter)
    fm["task_status"] = "expired"
    write_markdown(loop_path, fm, doc.body)
    index_single_record(loop_path, vault, conn)
    conn.commit()
    digest = generate_batch_review(vault, db)
    assert "REPEATEDLY EXPIRED (2x)" in digest


def test_dry_run_still_executes_nothing(world):
    vault, db, conn, config, scripts, tmp = world
    _script(scripts, "echo.sh", "#!/bin/sh\necho did the thing\n")
    _task_loop(vault, conn, "Run the echo", payload={"script": "echo.sh"})
    conn.close()
    config["adjutant"]["enabled"] = False
    spy = CaptureSpy()
    result = run_cycle(vault, db, config=config, capture=spy, scratch_root=tmp)
    assert result["dry_run"] is True
    assert result["executed"] == []
    assert spy.calls == []
