"""WO-ADJUTANT step 3: poller + gate + dry-run cycle.

The binding claims: selection is pure SQL against the index; disabled
arenas are never selected; every verdict lands in adjutant_log with its
rule and intent version; an invalid intent halts the cycle loudly; and
nothing — ever — executes in this step.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_gate import gate, required_capabilities
from lisan.tools.adjutant_poller import poll, priority_rank
from lisan.tools.adjutant_runner import adjutant_status, format_status, run_cycle
from lisan.tools.db import connect as db_connect
from lisan.tools.intent import init_intent, intent_path, load_intent
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.record_factory import new_confirmation, new_open_loop, new_schedule

DELEGATIONS = {
    "defaults": {"mode": "report_only"},
    "arenas": {
        "work": {
            "mode": "execute",
            "capabilities": ["run_local_scripts", "read_files", "write_files", "web_research", "send_outbound_message"],
            "confirm_required": ["git_push"],
        },
        "financial": {"mode": "disabled"},
    },
    "global": {
        "send_outbound_message": "confirm_always",
        "max_task_wall_seconds": 600,
        "max_tasks_per_cycle": 3,
    },
}

PRIORITIES = "1. Keep the backups healthy.\n2. Ship the lisan adjutant work.\n"


@pytest.fixture()
def world(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    init_intent(vault)
    # Install our delegations + priorities into the template.
    path = intent_path(vault)
    body_doc = load_markdown(path)
    body = body_doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(DELEGATIONS, indent=2) + "\n```" + body[end:]
    prio_start = body.index("# Priorities")
    prio_end = body.index("# Standing Delegations")
    body = body[:prio_start] + f"# Priorities\n\n{PRIORITIES}\n" + body[prio_end:]
    path.write_text(dump_markdown(body_doc.frontmatter, body), encoding="utf-8")
    # The fixture's direct write is a deliberate edit, not an out-of-band
    # surprise; record its hash so cycles start clean.
    from lisan.tools.intent import _record_known_hash

    _record_known_hash(vault)

    db = tmp_path / "adjutant.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    return vault, db, conn


def _task_loop(vault, conn, title, *, arena="work", kind="run_script", asap=True, due="", status="active", blocked=None):
    created = new_open_loop(vault, title, domain_primary=arena)
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(task_kind=kind, task_payload={}, task_status="pending", status=status)
    if asap:
        fm["execute_asap"] = True
    if due:
        fm["due"] = due
    if blocked:
        fm["blocked_contexts"] = blocked
    write_markdown(created.path, fm, doc.body)
    assert index_single_record(created.path, vault, conn)
    conn.commit()
    return fm["id"]


# ---------------------------------------------------------------------------
# Poller selection

def test_poller_selects_asap_and_due_not_future_or_untasked(world):
    vault, db, conn = world
    asap = _task_loop(vault, conn, "Asap thing", asap=True)
    due = _task_loop(vault, conn, "Due thing", asap=False, due="2026-07-01")
    _task_loop(vault, conn, "Future thing", asap=False, due="2027-01-01")
    plain = new_open_loop(vault, "Untasked loop", domain_primary="work")
    index_single_record(plain.path, vault, conn)
    conn.commit()
    ids = [t.task_id for t in poll(conn, load_intent(vault), vault, today="2026-07-23")]
    assert asap in ids and due in ids
    assert len(ids) == 2


def test_disabled_arena_is_never_selected(world):
    vault, db, conn = world
    _task_loop(vault, conn, "Forbidden finance task", arena="financial")
    visible = _task_loop(vault, conn, "Allowed work task")
    ids = [t.task_id for t in poll(conn, load_intent(vault), vault, today="2026-07-23")]
    assert ids == [visible]


def test_resolved_task_status_is_not_selected(world):
    vault, db, conn = world
    created = new_open_loop(vault, "Already ran", domain_primary="work")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(task_kind="draft", task_status="resolved", execute_asap=True)
    write_markdown(created.path, fm, doc.body)
    index_single_record(created.path, vault, conn)
    conn.commit()
    assert poll(conn, load_intent(vault), vault, today="2026-07-23") == []


def test_due_schedule_selected_future_not(world):
    vault, db, conn = world
    due = new_schedule(vault, "digest now", task_kind="draft", cron="daily@08:00", next_run="2026-07-23T08:00:00")
    future = new_schedule(vault, "digest later", task_kind="draft", cron="daily@08:00", next_run="2026-07-24T08:00:00")
    for record in (due, future):
        index_single_record(record.path, vault, conn)
    conn.commit()
    tasks = poll(conn, load_intent(vault), vault, today="2026-07-23", now="2026-07-23T12:00:00")
    assert [t.source for t in tasks] == ["schedule"]
    assert tasks[0].task_id == "schedule.digest-now"


def test_decision_pending_steps_polled_with_union_kinds(world):
    vault, db, conn = world
    from lisan.tools.record_factory import new_decision

    created = new_decision(vault, "Rotate backups quarterly", domain_primary="work")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm["execution_steps"] = [
        {"step": "draft the rotation doc", "task_kind": "draft", "status": "pending"},
        {"step": "collect old archives", "task_kind": "collect", "status": "pending"},
        {"step": "already done", "task_kind": "run_script", "status": "resolved"},
    ]
    write_markdown(created.path, fm, doc.body)
    index_single_record(created.path, vault, conn)
    conn.commit()
    tasks = poll(conn, load_intent(vault), vault, today="2026-07-23")
    assert len(tasks) == 1
    assert tasks[0].source == "decision"
    assert tasks[0].task_kinds == ["draft", "collect"]


def test_approved_confirmation_jumps_queue(world):
    vault, db, conn = world
    loop_id = _task_loop(vault, conn, "Ship the lisan adjutant work now")
    other = _task_loop(vault, conn, "Keep the backups healthy always")
    created = new_confirmation(
        vault, "confirm push", task_id=loop_id, task_summary="push it",
        planned_action="git push origin main", risk="public", expires="2026-07-30",
    )
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(status="resolved", resolution="approved", resolved_at="2026-07-23", resolved_by="owner")
    write_markdown(created.path, fm, doc.body)
    index_single_record(created.path, vault, conn)
    conn.commit()
    tasks = poll(conn, load_intent(vault), vault, today="2026-07-23")
    assert tasks[0].source == "confirmation" and tasks[0].task_id == loop_id
    assert other in [t.task_id for t in tasks]


def test_ordering_priority_match_then_due(world):
    vault, db, conn = world
    backups = _task_loop(vault, conn, "Keep the backups healthy: verify archive", asap=True)
    adjutant = _task_loop(vault, conn, "Ship the lisan adjutant gate", asap=True)
    unmatched_early = _task_loop(vault, conn, "Water the garden", asap=False, due="2026-07-01")
    unmatched_late = _task_loop(vault, conn, "Feed the fish", asap=False, due="2026-07-10")
    intent = load_intent(vault)
    tasks = poll(conn, intent, vault, today="2026-07-23")
    # Cap is 3 (max_tasks_per_cycle) — the latest unmatched task falls off.
    assert [t.task_id for t in tasks] == [backups, adjutant, unmatched_early]
    assert unmatched_late not in [t.task_id for t in tasks]


def test_priority_rank_is_token_overlap():
    priorities = ["keep the backups healthy", "ship the adjutant work"]
    assert priority_rank("verify backups tonight", priorities) == 0
    assert priority_rank("adjutant gate review", priorities) == 1
    assert priority_rank("unrelated errand", priorities) == 10**6


# ---------------------------------------------------------------------------
# Gate on polled tasks

def test_gate_maps_kinds_to_capabilities(world):
    vault, db, conn = world
    intent = load_intent(vault)
    assert required_capabilities(["run_script"]) == ["run_local_scripts", "read_files", "write_files"]
    v = gate({"arena": "work", "task_kinds": ["run_script"], "blocked_contexts": []}, intent)
    assert v.decision == "execute"
    v = gate({"arena": "work", "task_kinds": ["notify"], "blocked_contexts": []}, intent)
    assert v.decision == "confirm"  # global send_outbound_message=confirm_always
    v = gate({"arena": "somewhere", "task_kinds": ["draft"], "blocked_contexts": []}, intent)
    assert v.decision == "report_only"


def test_gate_denies_misfiled_task(world):
    vault, db, conn = world
    intent = load_intent(vault)
    v = gate({"arena": "work", "task_kinds": ["draft"], "blocked_contexts": ["work"]}, intent)
    assert v.decision == "deny"
    assert v.rule == "misfiled_task"


def test_gate_denies_unknown_kind(world):
    vault, db, conn = world
    intent = load_intent(vault)
    v = gate({"arena": "work", "task_kinds": ["transmute"], "blocked_contexts": []}, intent)
    assert v.decision == "deny" and v.rule == "unknown_task_kind"


# ---------------------------------------------------------------------------
# The cycle

def test_cycle_logs_every_verdict_with_intent_version(world):
    vault, db, conn = world
    _task_loop(vault, conn, "Ship the lisan adjutant gate")
    _task_loop(vault, conn, "Notify someone", kind="notify")
    conn.close()
    result = run_cycle(vault, db)
    assert not result["halted"] and result["dry_run"] is True
    decisions = {v["task_id"]: v["verdict"] for v in result["verdicts"]}
    assert set(decisions.values()) == {"execute", "confirm"}
    check = db_connect(db)
    check.row_factory = sqlite3.Row
    rows = check.execute("SELECT * FROM adjutant_log WHERE task_id != 'cycle'").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["intent_version"] == result["intent_version"]
        assert row["matched_rule"]
        assert json.loads(row["capabilities"])
    cycle = check.execute("SELECT * FROM adjutant_log WHERE verdict='cycle'").fetchone()
    assert "tasks=2" in cycle["note"]
    check.close()


def test_cycle_executes_nothing(world):
    vault, db, conn = world
    loop_id = _task_loop(vault, conn, "Ship the lisan adjutant gate")
    conn.close()
    before = {p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md")}
    run_cycle(vault, db)
    after = {p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md")}
    assert before == after  # dry-run touches no records
    check = db_connect(db)
    row = check.execute("SELECT task_status FROM files WHERE id=?", (loop_id,)).fetchone()
    assert row[0] == "pending"  # not marked running/resolved
    check.close()


def test_invalid_intent_halts_loudly(world):
    vault, db, conn = world
    conn.close()
    p = intent_path(vault)
    p.write_text(p.read_text(encoding="utf-8").replace("# Never", "# Whatever"), encoding="utf-8")
    result = run_cycle(vault, db)
    assert result["halted"] and "Never" in result["reason"]
    status = adjutant_status(vault, db)
    assert status["halted"] is not None
    assert "Never" in status["halted"]["reason"]
    assert not status["intent_valid"]
    rendered = format_status(status)
    assert "HALTED" in rendered


def test_out_of_band_intent_edit_is_absorbed_and_logged(world):
    vault, db, conn = world
    conn.close()
    run_cycle(vault, db)  # records the known hash
    p = intent_path(vault)
    p.write_text(p.read_text(encoding="utf-8").replace("Keep the backups healthy.", "Backups above all."), encoding="utf-8")
    result = run_cycle(vault, db)
    assert not result["halted"]
    check = db_connect(db)
    row = check.execute("SELECT * FROM adjutant_log WHERE verdict='intent_oob_edit'").fetchone()
    assert row is not None
    check.close()
    assert load_intent(vault).version == 2


def test_missing_intent_halts(world, tmp_path):
    vault2 = tmp_path / "vault2"
    ensure_vault_layout(vault2)
    db2 = tmp_path / "db2.sqlite"
    result = run_cycle(vault2, db2)
    assert result["halted"] and "intent init" in result["reason"]
