"""Applying a gated change (learning loop step 3): the careful last step.

The gate decided; the applier writes. It snapshots first, refuses plans made
against text that has since changed, writes atomically, verifies the result and
undoes it if it is bad, and logs everything. A failed apply must leave the skill
exactly as it was.
"""
from __future__ import annotations

import threading
from unittest.mock import patch as mock_patch

import pytest

from lisan.tools import skill_apply as A
from lisan.tools import skill_gate as G
from lisan.tools import skill_history as H
from lisan.tools.skill_format import parse_frontmatter
from lisan.tools.skill_loader import load_skills

OWNER = "---\nname: server-audit\ndescription: Use when auditing a server.\nversion: 1.0.0\n---\n\n# Audit\n\n1. check disks\n2. check memory\n"
EVENTS = {"e1": {"id": "e1", "conversation_id": "telegram-1", "sources": ["owner", "web"], "tainted": True},
          "e2": {"id": "e2", "conversation_id": "telegram-1", "sources": ["owner"], "tainted": False}}


@pytest.fixture()
def skills(tmp_path):
    root = tmp_path / "skills"
    (root / "server-audit").mkdir(parents=True)
    (root / "server-audit" / "SKILL.md").write_text(OWNER, encoding="utf-8")
    return root


def plan(skills, **kw):
    base = dict(op="patch", skill="server-audit", old_text="2. check memory", new_text="2. check memory and swap",
                evidence=["e1"], rationale="swap")
    base.update(kw)
    verdict = G.gate_operation(G.Operation(**base), skills_dir=skills, events=EVENTS, batch_ids=set(EVENTS), today="2026-10-02")
    assert verdict.accepted, verdict.reasons
    return verdict.change


def md(skills, name="server-audit"):
    return (skills / name / "SKILL.md").read_text(encoding="utf-8")


# ── a successful apply ──────────────────────────────────────────────────────

def test_a_patch_to_an_owner_skill_snapshots_the_owners_text_as_v0_then_writes(skills):
    change = plan(skills)
    applied = A.apply_change(change, skills_dir=skills, review_id="review-1", reason="swap was missed")
    assert "check memory and swap" in md(skills) and md(skills) == change.files["SKILL.md"]
    (entry,) = H.list_history(skills, "server-audit")
    assert entry["version_id"] == applied.snapshot and entry["label"] == "v0"
    assert (skills / ".history" / "server-audit" / applied.snapshot / "SKILL.md").read_text(encoding="utf-8") == OWNER
    assert applied.version_before == "1.0.0" and applied.version_after == "1.0.1"


def test_the_change_is_logged_with_its_evidence_provenance_and_review(skills):
    A.apply_change(plan(skills, evidence=["e1", "e2"]), skills_dir=skills, review_id="review-7", reason="why")
    applies = [e for e in H.read_log(skills, "server-audit") if e["action"] == "apply"]
    assert len(applies) == 1
    log = applies[0]
    assert log["review_id"] == "review-7" and log["actor"] == "reviewer:review-7" and log["op"] == "patch"
    assert log["events"] == ["e1", "e2"] and log["tainted"] is True and log["sources"] == ["owner", "web"]
    assert log["version"] == ["1.0.0", "1.0.1"] and log["snapshot"]


def test_one_command_returns_the_skill_to_the_owners_exact_text(skills):
    A.apply_change(plan(skills), skills_dir=skills, review_id="r")
    H.rollback_skill(skills, "server-audit", "v0")
    assert md(skills) == OWNER
    assert H.diff_skill(skills, "server-audit", since="owner") == ""


def test_a_new_skill_appears_whole_provisional_and_without_a_snapshot(skills):
    change = plan(skills, op="create", skill="log-rotation", description="Use when rotating logs.",
                  body="# Log rotation\n\n1. dry run first\n", evidence=["e1", "e2"], old_text="", new_text="")
    applied = A.apply_change(change, skills_dir=skills, review_id="r")
    assert applied.snapshot is None and (skills / "log-rotation" / "SKILL.md").is_file()
    data, body = parse_frontmatter(md(skills, "log-rotation"))
    assert data["metadata"]["origin"] == "agent" and data["metadata"]["status"] == "provisional"
    assert "log-rotation" in {s["name"] for s in load_skills(skills)}
    assert not [p for p in skills.iterdir() if p.name.startswith(".new-skill-")]  # no staging left behind


def test_add_reference_writes_both_files(skills):
    change = plan(skills, op="add_reference", file="references/disks.md", content="# Disks\n\ndf -h\n",
                  pointer="deeper disk checks", old_text="", new_text="")
    applied = A.apply_change(change, skills_dir=skills, review_id="r")
    assert sorted(applied.files) == ["SKILL.md", "references/disks.md"]
    assert (skills / "server-audit" / "references" / "disks.md").read_text(encoding="utf-8") == "# Disks\n\ndf -h\n"
    assert "references/disks.md" in md(skills)


def test_no_temp_files_are_left_behind_after_a_successful_apply(skills):
    A.apply_change(plan(skills), skills_dir=skills, review_id="r")
    leftovers = [p for p in (skills / "server-audit").rglob("*") if p.name.startswith(".apply-")]
    assert leftovers == []


# ── refusals leave the skill exactly as it was ──────────────────────────────

def test_a_plan_made_against_text_that_has_since_changed_is_refused_not_merged(skills):
    change = plan(skills)
    (skills / "server-audit" / "SKILL.md").write_text(OWNER + "\n3. the owner added this meanwhile\n", encoding="utf-8")
    edited = md(skills)
    with pytest.raises(A.ApplyRefused, match="changed after this change was planned"):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert md(skills) == edited  # the owner's edit survives untouched
    assert H.list_history(skills, "server-audit") == []  # and no snapshot was taken for a change that did not happen


def test_a_pin_placed_after_planning_is_respected(skills):
    change = plan(skills)
    H.pin_skill(skills, "server-audit")
    with pytest.raises(A.ApplyRefused, match="pinned"):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert md(skills) == OWNER


def test_a_new_skill_plan_is_refused_if_the_name_has_since_been_taken(skills):
    change = plan(skills, op="create", skill="log-rotation", description="Use when rotating logs.",
                  body="b", evidence=["e1", "e2"], old_text="", new_text="")
    (skills / "log-rotation").mkdir()
    (skills / "log-rotation" / "SKILL.md").write_text("---\nname: log-rotation\ndescription: d\n---\nmine\n", encoding="utf-8")
    with pytest.raises(A.ApplyRefused, match="now exists"):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert "mine" in md(skills, "log-rotation")


def test_a_skill_that_vanished_is_refused(skills):
    change = plan(skills)
    H.archive_skill(skills, "server-audit")
    with pytest.raises(A.ApplyRefused, match="no longer exists"):
        A.apply_change(change, skills_dir=skills, review_id="r")


def test_a_result_that_is_not_a_valid_skill_is_undone(skills):
    change = plan(skills)
    change.files["SKILL.md"] = "no frontmatter at all, so not a skill\n"
    with pytest.raises(A.ApplyRefused, match="valid skill"):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert md(skills) == OWNER  # restored from the snapshot


def test_an_invalid_new_skill_is_never_left_on_disk(skills):
    change = plan(skills, op="create", skill="log-rotation", description="Use when rotating logs.",
                  body="b", evidence=["e1", "e2"], old_text="", new_text="")
    change.files["SKILL.md"] = "not a skill\n"
    with pytest.raises(A.ApplyRefused):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert not (skills / "log-rotation").exists()
    assert not [p for p in skills.iterdir() if p.name.startswith(".new-skill-")]


def test_a_failure_midway_through_several_files_restores_the_skill(skills):
    change = plan(skills, op="add_reference", file="references/disks.md", content="# Disks\n",
                  pointer="p", old_text="", new_text="")
    real = A._atomic_write
    calls = []

    def flaky(path, text):
        calls.append(path.name)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, text)

    with mock_patch("lisan.tools.skill_apply._atomic_write", flaky):
        with pytest.raises(OSError):
            A.apply_change(change, skills_dir=skills, review_id="r")
    assert md(skills) == OWNER  # SKILL.md was written first, then restored


# ── concurrency ─────────────────────────────────────────────────────────────

def test_two_applies_to_one_skill_serialise_and_the_second_sees_a_stale_plan(skills):
    first, second = plan(skills), plan(skills, old_text="1. check disks", new_text="1. check disks and inodes")
    outcomes = []
    barrier = threading.Barrier(2)

    def run(change):
        barrier.wait()
        try:
            A.apply_change(change, skills_dir=skills, review_id="r")
            outcomes.append("applied")
        except A.ApplyRefused:
            outcomes.append("refused")

    threads = [threading.Thread(target=run, args=(c,)) for c in (first, second)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["applied", "refused"]  # never both, never interleaved
    assert parse_frontmatter(md(skills))[0]["version"] == "1.0.1"


def test_applies_to_different_skills_both_succeed(skills):
    (skills / "dns-checks").mkdir()
    (skills / "dns-checks" / "SKILL.md").write_text("---\nname: dns-checks\ndescription: Use when debugging DNS.\n---\n\n1. dig it\n", encoding="utf-8")
    a = plan(skills)
    b = plan(skills, skill="dns-checks", old_text="1. dig it", new_text="1. dig it +trace")
    for change in (a, b):
        A.apply_change(change, skills_dir=skills, review_id="r")
    assert "swap" in md(skills) and "+trace" in md(skills, "dns-checks")
