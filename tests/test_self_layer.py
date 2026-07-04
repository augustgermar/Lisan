"""WO-4: Layer B — deterministic self-episodes and capability beliefs.

The anti-confabulation property is structural: episodes are template
renderings of real job rows and ceremony artifacts, every episode carries
source_refs, backfill is idempotent, and a belief revision without
evidence refs is refused.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lisan.frontmatter import load_markdown
from lisan.tools.self_beliefs import list_self_beliefs, new_self_belief, revise_self_belief
from lisan.tools.self_episodes import (
    assemble_self_episodes,
    ceremony_events,
    collect_events,
    job_events,
    record_job_episode,
)


def _seed_jobs_db(tmp_path: Path) -> Path:
    db = tmp_path / "lisan.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_type TEXT, status TEXT, "
        "payload_json TEXT, result_json TEXT, finished_at TEXT, error TEXT, attempts INTEGER)"
    )
    rows = [
        (1, "task.reminder", "succeeded", json.dumps({"text": "water the wisteria"}), "{}",
         "2026-07-01T09:00:00", None, 1),
        (2, "plan.run", "succeeded", json.dumps({"title": "inventory the shed"}), "{}",
         "2026-07-02T10:00:00", None, 1),
        (3, "task.prompt", "failed", json.dumps({"prompt": "summarize open loops"}), None,
         "2026-07-03T11:00:00", "provider timeout", 3),
        (4, "capture.observe", "succeeded", "{}", "{}", "2026-07-03T12:00:00", None, 1),
    ]
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_job_events_cover_biography_grade_only(tmp_path):
    events = job_events(_seed_jobs_db(tmp_path))
    assert {e.event_id for e in events} == {"job-1", "job-2", "job-3"}  # not capture.observe
    reminder = next(e for e in events if e.event_id == "job-1")
    assert "water the wisteria" in reminder.narration
    assert "{{self}}" in reminder.narration and "{{principal}}" in reminder.narration
    failure = next(e for e in events if e.event_id == "job-3")
    assert failure.outcome == "failed"
    assert "provider timeout" in failure.narration
    assert failure.significance == "medium"


def test_ceremony_and_drift_events(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "voice-extraction-20260704111100.md").write_text("---\n{}\n---\nx", encoding="utf-8")
    (reports / "kernel-drift.md").write_text(
        "# Kernel drift events\n\n- 2026-07-05T08:00:00-07:00 — kernel content changed outside a ceremony.\n",
        encoding="utf-8",
    )
    events = ceremony_events(tmp_path)
    kinds = {e.event_kind for e in events}
    assert kinds == {"ceremony", "drift"}
    ceremony = next(e for e in events if e.event_kind == "ceremony")
    assert ceremony.date == "2026-07-04"
    assert ceremony.source_refs == ["reports/voice-extraction-20260704111100.md"]


def test_assemble_is_idempotent_with_source_refs(tmp_path):
    db = _seed_jobs_db(tmp_path)
    first = assemble_self_episodes(tmp_path, db)
    assert first["written"] == 3
    second = assemble_self_episodes(tmp_path, db)
    assert second["written"] == 0  # run twice, count once
    files = sorted((tmp_path / "self" / "episodes").glob("*.md"))
    assert len(files) == 3
    for path in files:
        doc = load_markdown(path)
        assert doc.frontmatter["type"] == "self_episode"
        assert doc.frontmatter["source_refs"]
        assert "## Sources" in doc.body


def test_assembled_episodes_pass_the_validator(tmp_path):
    db = _seed_jobs_db(tmp_path)
    assemble_self_episodes(tmp_path, db)
    from lisan.tools.validator import ENUMS

    for path in (tmp_path / "self" / "episodes").glob("*.md"):
        fm = load_markdown(path).frontmatter
        assert fm["type"] in ENUMS["type"]
        assert fm["outcome"] in {"succeeded", "failed", "ratified", "drifted"}


def test_record_job_episode_hook(tmp_path):
    db = _seed_jobs_db(tmp_path)
    record_job_episode(tmp_path, {"id": 1, "job_type": "task.reminder"}, db_path=db)
    files = list((tmp_path / "self" / "episodes").glob("*.md"))
    assert len(files) == 1
    record_job_episode(tmp_path, {"id": 4, "job_type": "capture.observe"}, db_path=db)  # not biography-grade
    assert len(list((tmp_path / "self" / "episodes").glob("*.md"))) == 1


# ── Beliefs ──────────────────────────────────────────────────────────────────


def test_new_belief_and_listing(tmp_path):
    path = new_self_belief(tmp_path, "I am good at holding a conversational thread.",
                           confidence="medium", evidence_refs=["jobs:2"])
    fm = load_markdown(path).frontmatter
    assert fm["type"] == "self_belief"
    assert fm["belief_confidence"] == "medium"
    assert fm["revisions"] == []
    beliefs = list_self_beliefs(tmp_path)
    assert len(beliefs) == 1


def test_revision_chains_and_never_overwrites_silently(tmp_path):
    path = new_self_belief(tmp_path, "I am not very good at telling stories.", confidence="medium")
    revise_self_belief(
        path,
        new_statement="I can tell a story well when I have real material.",
        new_confidence="medium",
        reason="three long-form narratives landed",
        evidence_refs=["self_episode.job-2"],
    )
    doc = load_markdown(path)
    assert doc.frontmatter["summary"] == "I can tell a story well when I have real material."
    revisions = doc.frontmatter["revisions"]
    assert len(revisions) == 1
    assert revisions[0]["previous_statement"] == "I am not very good at telling stories."
    assert "I believed" in doc.body
    assert "not very good at telling stories" in doc.body  # the old belief stays visible


def test_revision_without_evidence_is_refused(tmp_path):
    path = new_self_belief(tmp_path, "I never lose the thread.")
    with pytest.raises(ValueError, match="evidence"):
        revise_self_belief(path, new_statement="I sometimes lose the thread.",
                           new_confidence="low", reason="vibes", evidence_refs=[])


def test_belief_files_reject_duplicates(tmp_path):
    new_self_belief(tmp_path, "I am careful with dates.")
    with pytest.raises(FileExistsError):
        new_self_belief(tmp_path, "I am careful with dates.")
