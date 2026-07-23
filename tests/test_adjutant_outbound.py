"""WO-ADJUTANT step 5: research + notify, via the existing Telegram path.

Binding claims: research findings carry per-finding sources and
confidence (unsourced findings are forced low); a notify confirmation
contains the FULL outgoing text — the human approves the message, not a
summary; an approved notify re-gates at send time and loses to a
never-rule added since; the bot's approve/deny commands resolve
deterministically before any model sees them.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_confirmations import (
    confirmation_command_response,
    create_confirmation_for_task,
    expire_stale_confirmations,
    list_pending,
)
from lisan.tools.adjutant_executor import execute_notify, execute_research
from lisan.tools.adjutant_runner import run_cycle
from lisan.tools.db import connect as db_connect
from lisan.tools.intent import _record_known_hash, init_intent, intent_path
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.record_factory import new_open_loop

DELEGATIONS = {
    "defaults": {"mode": "report_only"},
    "arenas": {
        "work": {
            "mode": "execute",
            "capabilities": ["read_files", "write_files", "web_research", "send_outbound_message"],
        },
    },
    "global": {
        "send_outbound_message": "confirm_always",
        "max_task_wall_seconds": 30,
        "max_tasks_per_cycle": 5,
    },
}

RESEARCH_RESPONSE = """Here is what I found.

```json
{
  "findings": [
    {"finding": "The archive format changed in 2024.", "sources": ["https://example.org/changelog"], "confidence": "high"},
    {"finding": "Restores were untested for a year.", "sources": [], "confidence": "high"},
    {"finding": "", "sources": [], "confidence": "high"}
  ]
}
```"""


def _install_intent(vault, delegations):
    path = intent_path(vault)
    doc = load_markdown(path)
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
    fm = dict(doc.frontmatter)
    fm.update(created="2026-07-23", updated="2026-07-23", review_after="2026-10-23")
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    _record_known_hash(vault)


@pytest.fixture()
def world(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    init_intent(vault)
    _install_intent(vault, DELEGATIONS)
    db = tmp_path / "adjutant.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    config = {"adjutant": {"enabled": True, "script_dirs": [], "collect_paths": []}}
    return vault, db, conn, config


def _task_loop(vault, conn, title, *, kind, payload):
    created = new_open_loop(vault, title, domain_primary="work")
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update(task_kind=kind, task_payload=payload, task_status="pending", execute_asap=True)
    write_markdown(created.path, fm, doc.body)
    assert index_single_record(created.path, vault, conn)
    conn.commit()
    return fm["id"], created.path


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return {"captured": True}


# ---------------------------------------------------------------------------
# Research

def test_research_parses_findings_and_forces_unsourced_low():
    result = execute_research("t", {"question": "backup posture?"}, complete=lambda p: RESEARCH_RESPONSE)
    assert result.ok
    assert len(result.findings) == 2  # empty finding dropped
    assert result.findings[0]["confidence"] == "high"
    assert result.findings[1]["confidence"] == "low"  # unsourced forced down
    assert any("forced to low" in a for a in result.actions)


def test_research_failure_modes():
    result = execute_research("t", {}, complete=lambda p: "x")
    assert not result.ok and "no question" in result.errors[0]
    result = execute_research("t", {"question": "q"}, complete=None)
    assert not result.ok and "provider" in result.errors[0]
    result = execute_research("t", {"question": "q"}, complete=lambda p: "no json here")
    assert not result.ok and "no fenced JSON" in result.errors[0]
    result = execute_research("t", {"question": "q"}, complete=lambda p: "```json\n{broken\n```")
    assert not result.ok and "not valid JSON" in result.errors[0]


def test_research_task_gated_and_reported_through_capture(world):
    vault, db, conn, config = world
    loop_id, _ = _task_loop(vault, conn, "Research the archive format", kind="research", payload={"question": "what changed?"})
    conn.close()
    captured = []

    def capture(**kw):
        captured.append(kw)
        return {}

    result = run_cycle(vault, db, config=config, complete=lambda p: RESEARCH_RESPONSE, capture=capture)
    assert result["executed"][0]["ok"]
    text = captured[0]["text"]
    assert "The archive format changed in 2024" in text
    assert captured[0]["conversation_id"] == "adjutant"


# ---------------------------------------------------------------------------
# Notify

def test_notify_sends_exact_payload_text():
    sent = []
    result = execute_notify("t", {"message": "Invoice #42 is due Friday."}, deliver=sent.append)
    assert result.ok and sent == ["Invoice #42 is due Friday."]
    result = execute_notify("t", {"message": "x"}, deliver=None)
    assert not result.ok and "no delivery channel" in result.errors[0]
    result = execute_notify("t", {}, deliver=sent.append)
    assert not result.ok and "no message" in result.errors[0]


def test_notify_confirmation_contains_full_message(world):
    vault, db, conn, config = world
    message = "Hello owner. The weekly digest is ready:\n- backups green\n- 2 loops open"
    loop_id, _ = _task_loop(vault, conn, "Send the digest", kind="notify", payload={"message": message})
    conn.close()
    sent = []
    result = run_cycle(vault, db, config=config, capture=Spy(), deliver=sent.append)
    assert result["verdicts"][0]["verdict"] == "confirm"
    pending = list_pending(db)
    assert len(pending) == 1
    record = load_markdown(vault / pending[0]["record_path"])
    # Spec §5 / Never #1: the human approves the actual message.
    assert message in record.frontmatter["planned_action"]
    # The owner ping about the pending confirmation also carries it in full.
    assert any(message in s for s in sent)


def test_approved_notify_sends_then_regate_blocks_after_intent_change(world):
    vault, db, conn, config = world
    message = "Quarterly letter: all is well."
    loop_id, loop_path = _task_loop(vault, conn, "Send the letter", kind="notify", payload={"message": message})
    conn.close()
    sent = []
    run_cycle(vault, db, config=config, capture=Spy(), deliver=sent.append)
    pending = list_pending(db)
    reply = confirmation_command_response(vault, f"approve {pending[0]['id']}", db, capture=Spy())
    assert reply and "Approved" in reply

    # Owner slams the door before the next cycle: outbound becomes never.
    tightened = json.loads(json.dumps(DELEGATIONS))
    tightened["global"]["send_outbound_message"] = "never"
    _install_intent(vault, tightened)

    sent.clear()
    result = run_cycle(vault, db, config=config, capture=Spy(), deliver=sent.append)
    assert result["executed"] == []
    assert sent == []  # the approved message was NOT sent
    check = db_connect(db)
    assert check.execute("SELECT 1 FROM adjutant_log WHERE verdict='approval_overridden'").fetchone()
    check.close()


def test_approved_notify_sends_when_intent_still_allows(world):
    vault, db, conn, config = world
    message = "Quarterly letter: all is well."
    loop_id, loop_path = _task_loop(vault, conn, "Send the letter", kind="notify", payload={"message": message})
    conn.close()
    sent = []
    run_cycle(vault, db, config=config, capture=Spy(), deliver=sent.append)
    pending = list_pending(db)
    confirmation_command_response(vault, f"approve {pending[0]['id']}", db, capture=Spy())
    sent.clear()
    result = run_cycle(vault, db, config=config, capture=Spy(), deliver=sent.append)
    assert len(result["executed"]) == 1 and result["executed"][0]["ok"]
    assert sent == [message]  # exactly the approved text, nothing else
    assert load_markdown(loop_path).frontmatter["task_status"] == "resolved"


# ---------------------------------------------------------------------------
# Bot command handling (deterministic, before any model)

def test_confirmation_commands_resolve_deterministically(world):
    vault, db, conn, config = world
    loop_id, _ = _task_loop(vault, conn, "Push it", kind="notify", payload={"message": "m"})
    created = create_confirmation_for_task(
        vault, task_id=loop_id, task_summary="push", planned_action="m", risk="outbound", db_path=db
    )
    assert confirmation_command_response(vault, "what should I cook tonight?", db) is None
    listing = confirmation_command_response(vault, "/confirmations", db)
    assert created in listing
    reply = confirmation_command_response(vault, f"deny {created}", db, capture=Spy())
    assert "Denied" in reply and loop_id in reply
    reply = confirmation_command_response(vault, f"deny {created}", db, capture=Spy())
    assert "Cannot deny" in reply  # already resolved
    assert confirmation_command_response(vault, "/confirmations", db) == "No pending confirmations."


def test_double_expiry_pings_owner_exactly_once(world):
    vault, db, conn, config = world
    loop_id, loop_path = _task_loop(vault, conn, "Send the letter", kind="notify", payload={"message": "m"})
    pings = []
    for round_ in range(3):
        created = create_confirmation_for_task(
            vault, task_id=loop_id, task_summary="send", planned_action="m", risk="outbound", db_path=db
        )
        assert created
        # Force expiry by backdating the record.
        row_path = [p["record_path"] for p in list_pending(db)][0]
        doc = load_markdown(vault / row_path)
        fm = dict(doc.frontmatter)
        fm["expires"] = "2020-01-01"
        write_markdown(vault / row_path, fm, doc.body)
        from lisan.tools.rebuild_index import reindex_record

        reindex_record(vault / row_path, vault, db)
        result = run_cycle(vault, db, config=config, capture=Spy(), deliver=pings.append)
        # Re-arm the loop for the next round.
        doc = load_markdown(loop_path)
        fm = dict(doc.frontmatter)
        fm["task_status"] = "pending"
        write_markdown(loop_path, fm, doc.body)
        reindex_record(loop_path, vault, db)
    escalations = [p for p in pings if "keeps not getting made" in p]
    assert len(escalations) == 1  # exactly at the second expiry, not the third
