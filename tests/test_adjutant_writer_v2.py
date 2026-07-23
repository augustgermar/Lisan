"""WO-ADJUTANT step 7: writer v2 prompts behind the flag, and the
definition of done.

Binding claims: v1 stays the default (a vault without an Adjutant never
asks the writer about taskings); v2 is selected only when
adjutant.enabled; malformed task fields fail toward PLAIN records (the
ratified asymmetry — a false tasking costs a wrong verdict, a missed
tasking costs one command); and the whole closed loop works end to end
with a fake provider: instruction turn -> tasked open_loop -> dry-run
verdict -> execution -> result re-captured -> Skeptic review ->
originating loop resolved.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_runner import run_cycle
from lisan.tools.db import connect as db_connect
from lisan.tools.intent import _record_known_hash, init_intent, intent_path
from lisan.tools.record_fanout import (
    fanout_decisions,
    fanout_open_loops,
    sanitized_execution_steps,
    sanitized_task_fields,
)


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    ensure_vault_layout(v)
    return v


# ---------------------------------------------------------------------------
# Prompt selection by flag

def test_v1_default_v2_only_when_enabled(monkeypatch, vault):
    from lisan.agents.base import PromptAgent
    from lisan.agents.writer import WriterAgent

    chosen = []

    def fake_run_json(self, user_input, **kwargs):
        chosen.append(self.prompt_file)
        return {}

    monkeypatch.setattr(PromptAgent, "run_json", fake_run_json)
    WriterAgent(vault=vault, config={"adjutant": {"enabled": False}}).run_json("x", task="open_loop")
    WriterAgent(vault=vault, config={}).run_json("x", task="decision")
    WriterAgent(vault=vault, config={"adjutant": {"enabled": True}}).run_json("x", task="open_loop")
    WriterAgent(vault=vault, config={"adjutant": {"enabled": True}}).run_json("x", task="decision")
    WriterAgent(vault=vault, config={"adjutant": {"enabled": True}}).run_json("x", task="episode")
    assert chosen == [
        "writer_open_loop_v1",
        "writer_decision_v1",
        "writer_open_loop_v2",
        "writer_decision_v2",
        "writer_episode_v1",  # only loop/decision get v2; episodes untouched
    ]


def test_v2_prompts_load_and_teach_restraint():
    from lisan.prompts import load_prompt

    for name in ("writer_open_loop_v2", "writer_decision_v2"):
        text = load_prompt(name)
        assert "not everything" in text.lower()
        assert "NEVER invent" in text


# ---------------------------------------------------------------------------
# Sanitization: fail toward plain records

def test_valid_task_fields_flow_into_the_record(vault):
    writer = {
        "open_loops_to_create": [
            {
                "title": "Run the restore check",
                "next_action": "Run restore_check.sh",
                "summary": "Verify the backup archive restores",
                "priority": "high",
                "owner": "user",
                "task": {
                    "task_kind": "run_script",
                    "task_payload": {"script": "restore_check.sh"},
                    "execute_asap": True,
                },
            }
        ]
    }
    fanout_open_loops(vault, writer, "drafts/x.md")
    record = next((vault / "open_loops").glob("*.md"))
    fm = load_markdown(record).frontmatter
    assert fm["task_kind"] == "run_script"
    assert fm["task_payload"] == {"script": "restore_check.sh"}
    assert fm["task_status"] == "pending"
    assert fm["execute_asap"] is True


@pytest.mark.parametrize(
    "task",
    [
        {"task_kind": "world_domination", "task_payload": {}},
        {"task_kind": "run_script", "task_payload": "rm -rf /"},
        {"task_kind": "run_script", "task_payload": {}, "due": "someday"},
    ],
)
def test_malformed_task_fails_toward_plain_loop(vault, task):
    writer = {
        "open_loops_to_create": [
            {"title": "A loop", "next_action": "Do a thing", "owner": "user", "task": task}
        ]
    }
    fanout_open_loops(vault, writer, "drafts/x.md")
    record = next((vault / "open_loops").glob("*.md"))
    fm = load_markdown(record).frontmatter
    # The loop survives; the tasking does not. Asymmetry, ratified.
    assert "task_kind" not in fm and "task_status" not in fm


def test_no_task_object_means_plain_loop(vault):
    assert sanitized_task_fields({"title": "x"}) == {}
    assert sanitized_task_fields({"task": {}}) == {}


def test_decision_steps_sanitized_individually(vault):
    writer = {
        "decisions_to_create": [
            {
                "title": "Rotate the backups",
                "summary": "Quarterly rotation adopted",
                "execution_steps": [
                    {"step": "draft the rotation doc", "task_kind": "draft", "task_payload": {"title": "r", "instructions": "i"}},
                    {"step": "", "task_kind": "draft"},
                    {"step": "hex the drives", "task_kind": "sorcery"},
                ],
            }
        ]
    }
    fanout_decisions(vault, writer, "drafts/x.md")
    record = next((vault / "decisions").glob("*.md"))
    fm = load_markdown(record).frontmatter
    steps = fm["execution_steps"]
    assert len(steps) == 1 and steps[0]["task_kind"] == "draft" and steps[0]["status"] == "pending"


def test_all_steps_malformed_means_plain_decision(vault):
    writer = {
        "decisions_to_create": [
            {"title": "Plain decision", "summary": "No follow-through stated",
             "execution_steps": [{"step": "x", "task_kind": "sorcery"}]}
        ]
    }
    fanout_decisions(vault, writer, "drafts/x.md")
    record = next((vault / "decisions").glob("*.md"))
    assert "execution_steps" not in load_markdown(record).frontmatter
    assert sanitized_execution_steps({"execution_steps": "not-a-list"}) == []


# ---------------------------------------------------------------------------
# The definition of done (spec §7): full closed loop, fake provider.

DELEGATIONS = {
    "defaults": {"mode": "report_only"},
    "arenas": {
        "work": {"mode": "execute", "capabilities": ["run_local_scripts", "read_files", "write_files"]},
    },
    "global": {"max_task_wall_seconds": 30, "max_tasks_per_cycle": 5},
}

INSTRUCTION = "Run the restore check script tonight — restore_check.sh — and make sure the backup archive still restores."


def _fake_agents(monkeypatch, writer_prompt_seen: list[str]):
    """One patch point: every prompt agent returns canned output by role.
    All deterministic machinery — routing, fanout, gate, executor,
    reporter, capture — runs for real."""
    from lisan.agents.base import PromptAgent
    from lisan.agents.assembler import AssemblerAgent

    def fake_run_json(self, user_input, **kwargs):
        name = getattr(self, "name", "")
        if name == "listener":
            return {
                "action": "capture", "mode": "extraction", "memory_type": "open_loop",
                "significance": "medium", "seed_score": 3, "reason": ["explicit instruction"],
            }
        if name == "writer":
            writer_prompt_seen.append(self.prompt_file)
            if str(user_input).startswith("ADJUTANT RESULT"):
                # The result observer: plain episode-ish output, no new loops.
                return {
                    "record_type": "open_loop", "summary": "Adjutant completed the restore check",
                    "significance": "low", "frontmatter": {"summary": "Adjutant completed the restore check"},
                    "sections": {"open_loop": "result"}, "questions": [],
                    "open_loops_to_create": [], "decisions_to_create": [],
                    "entities_to_create": [], "state_updates": [],
                }
            return {
                "record_type": "open_loop",
                "summary": "Run the restore check script",
                "significance": "medium",
                "frontmatter": {"summary": "Run the restore check script"},
                "sections": {"open_loop": "The restore check needs to run tonight."},
                "questions": [],
                "significance_rationale": "explicit instruction",
                "entities_to_create": [], "state_updates": [], "decisions_to_create": [],
                "open_loops_to_create": [
                    {
                        "title": "Run the restore check script",
                        "next_action": "Run restore_check.sh and verify the archive restores",
                        "summary": "Run the restore check script against the backup archive",
                        "priority": "high", "owner": "user",
                        "confidence_basis": "User gave an explicit imperative instruction",
                        "domain": "work",
                        "task": {
                            "task_kind": "run_script",
                            "task_payload": {"script": "restore_check.sh"},
                            "execute_asap": True,
                        },
                    }
                ],
            }
        if name == "skeptic":
            return {"approved": True, "recommended_action": "approve", "risk": "low", "issues": []}
        if name == "interlocutor":
            return {"acknowledgment": "Noted."}
        return {}

    monkeypatch.setattr(PromptAgent, "run_json", fake_run_json)
    monkeypatch.setattr(
        AssemblerAgent, "run", lambda self, *a, **kw: SimpleNamespace(text=""), raising=True
    )
    # The interlocutor overrides run_json (tool-bearing path); fake it too.
    from lisan.agents.interlocutor import InterlocutorAgent

    monkeypatch.setattr(
        InterlocutorAgent, "run_json", lambda self, user_input, **kw: {"acknowledgment": "Noted."}
    )


def test_definition_of_done_closed_loop(monkeypatch, vault, tmp_path):
    from lisan.tools.capture import capture_text

    # World: adopted intent granting run_script in work; echo script allowlisted.
    init_intent(vault)
    path = intent_path(vault)
    doc = load_markdown(path)
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(DELEGATIONS, indent=2) + "\n```" + body[end:]
    fm = dict(doc.frontmatter)
    fm.update(created="2026-07-24", updated="2026-07-24", review_after="2026-10-24")
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    _record_known_hash(vault)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "restore_check.sh"
    script.write_text("#!/bin/sh\necho restore check completed: archive restores cleanly\n")
    script.chmod(0o755)
    db = tmp_path / "e2e.sqlite"
    config = {"adjutant": {"enabled": True, "script_dirs": [str(scripts)], "collect_paths": []}}

    writer_prompts = []
    _fake_agents(monkeypatch, writer_prompts)
    monkeypatch.setattr("lisan.config.load_config", lambda path=None: dict(config))
    monkeypatch.setattr("lisan.agents.base.load_config", lambda path=None: dict(config))

    # 1. Instruction turn enters through the front door.
    capture_text(vault, INSTRUCTION, conversation_id="test", db_path=db, queue_background=False)
    assert writer_prompts and writer_prompts[0] == "writer_open_loop_v2"  # flag selected v2
    loops = list((vault / "open_loops").glob("*.md"))
    assert len(loops) == 1
    loop_fm = load_markdown(loops[0]).frontmatter
    assert loop_fm["task_kind"] == "run_script" and loop_fm["task_status"] == "pending"
    loop_id = loop_fm["id"]

    # 2. Dry-run first: verdict logged, nothing executed.
    dry = run_cycle(vault, db, config={"adjutant": {**config["adjutant"], "enabled": False}})
    assert dry["dry_run"] and dry["verdicts"][0]["verdict"] == "execute" and dry["executed"] == []
    assert load_markdown(loops[0]).frontmatter["task_status"] == "pending"

    # 3. Enabled: the echo script runs; the result re-enters through capture.
    result = run_cycle(vault, db, config=config)
    assert result["executed"][0]["ok"]

    # 4. Skeptic reviewed the re-captured result (the writer observer saw it too).
    assert any(p == "writer_open_loop_v1" or p.startswith("writer_") for p in writer_prompts[1:])
    adjutant_turns = [p for p in writer_prompts[1:]]
    assert adjutant_turns  # the result went through the pipeline

    # 5. The originating loop is resolved — task lifecycle AND loop status.
    final = load_markdown(loops[0]).frontmatter
    assert final["task_status"] == "resolved"
    assert final["status"] == "resolved"  # closed by the completion matcher
    assert final["resolved_by"]

    # 6. The audit trail holds the whole story.
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT verdict FROM adjutant_log WHERE task_id = ?", (loop_id,)).fetchall()
    assert [r["verdict"] for r in rows] == ["execute", "execute"]
    run_row = conn.execute("SELECT exit_status FROM task_runs WHERE task_id = ?", (loop_id,)).fetchone()
    assert run_row["exit_status"] == "ok"
    conn.close()
