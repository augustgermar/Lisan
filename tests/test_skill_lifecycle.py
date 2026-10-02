"""Probation: agent-written skills earn trust by being used (learning step 3)."""
from __future__ import annotations

import calendar
import time

import pytest

from lisan.tools import learning as L
from lisan.tools import skill_history as H
from lisan.tools import skill_lifecycle as LC
from lisan.tools.skill_format import parse_frontmatter

NOW = calendar.timegm(time.strptime("2026-10-02", "%Y-%m-%d"))
AGENT_SKILL = ("---\nname: {name}\ndescription: Use when testing.\nversion: 0.1.0\nmetadata:\n  origin: agent\n"
               "  status: {status}\n  created: {created}\n---\n\nbody\n")
OWNER_SKILL = "---\nname: owner-skill\ndescription: Use when owning.\nversion: 1.0.0\n---\n\nbody\n"


def uses(*outcomes):
    return [(f"2026-10-0{i + 1}T00:00:00Z", o) for i, o in enumerate(outcomes)]


# ── the decision, as a pure function ────────────────────────────────────────

@pytest.mark.parametrize("status,created,outcomes,expected", [
    ("provisional", "2026-09-25", ["no_error_seen"] * 3, ("established", "used 3 times without error")),
    ("provisional", "2026-09-25", ["no_error_seen"] * 2, None),                      # not enough use yet
    ("provisional", "2026-10-01", ["no_error_seen"] * 3, None),                      # too young: needs 2 days
    ("provisional", "2026-09-25", ["tool_error", "no_error_seen", "no_error_seen", "no_error_seen"], ("established", "used 3 times without error")),
    ("provisional", "2026-09-25", ["no_error_seen", "no_error_seen", "tool_error", "tool_error"], ("flagged", "its last 2 uses failed")),
    ("provisional", "2026-09-25", ["tool_error", "no_error_seen"], None),            # a success breaks the run of failures
    ("provisional", "2026-09-25", ["no_error_seen"] * 3 + ["tool_error"], None),     # one failure is not enough to flag or to promote
    ("provisional", "2026-09-25", [], None),
    ("established", "2026-09-01", ["no_error_seen"] * 5 + ["tool_error"] * 2, ("flagged", "its last 2 uses failed")),
    ("established", "2026-09-01", ["no_error_seen"] * 5, None),
    ("flagged", "2026-09-01", ["tool_error"] * 4, None),                             # already flagged; stays until revised or approved
])
def test_decide(status, created, outcomes, expected):
    prov = {"status": status, "created": created}
    assert LC.decide(prov, uses(*outcomes), NOW) == expected


def test_a_skill_with_no_created_date_is_not_blocked_from_promotion():
    assert LC.decide({"status": "provisional", "created": None}, uses(*["no_error_seen"] * 3), NOW)[0] == "established"


# ── applying it to skills on disk ───────────────────────────────────────────

@pytest.fixture()
def env(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    db = tmp_path / "learn.sqlite"
    return type("E", (), {"skills": skills, "db": db, "tmp": tmp_path})


def make(env, name, *, status="provisional", created="2026-09-20", origin="agent"):
    (env.skills / name).mkdir()
    text = AGENT_SKILL.format(name=name, status=status, created=created) if origin == "agent" else OWNER_SKILL.replace("owner-skill", name)
    (env.skills / name / "SKILL.md").write_text(text, encoding="utf-8")


def use(env, name, outcomes):
    for i, outcome in enumerate(outcomes):
        conn = L._connect(env.db)
        conn.execute("INSERT INTO skill_usage (skill, skill_kind, event_id, used_at, outcome) VALUES (?,?,?,?,?)",
                     (name, "instructional", f"e.{name}.{i}", f"2026-09-{21 + i:02d}T00:00:00Z", outcome))
        conn.commit()
        conn.close()


def status(env, name):
    return parse_frontmatter((env.skills / name / "SKILL.md").read_text(encoding="utf-8"))[0]["metadata"]["status"]


def test_a_skill_that_has_proven_itself_is_promoted_with_a_snapshot_and_a_log_entry(env):
    make(env, "log-rotation")
    use(env, "log-rotation", ["no_error_seen"] * 3)
    (change,) = LC.evaluate_lifecycle(env.skills, env.db, now=NOW)
    assert change == {"skill": "log-rotation", "from": "provisional", "to": "established", "reason": "used 3 times without error"}
    assert status(env, "log-rotation") == "established"
    assert [e["action"] for e in H.read_log(env.skills, "log-rotation")] == ["snapshot", "promote"]
    assert H.list_history(env.skills, "log-rotation")  # a status change is undoable too


def test_a_failing_skill_is_flagged_never_deleted(env):
    make(env, "dns-checks")
    use(env, "dns-checks", ["no_error_seen", "tool_error", "tool_error"])
    (change,) = LC.evaluate_lifecycle(env.skills, env.db, now=NOW)
    assert change["to"] == "flagged" and status(env, "dns-checks") == "flagged"
    assert (env.skills / "dns-checks" / "SKILL.md").is_file()


def test_dry_run_reports_without_changing_anything(env):
    make(env, "log-rotation")
    use(env, "log-rotation", ["no_error_seen"] * 3)
    assert LC.evaluate_lifecycle(env.skills, env.db, apply=False, now=NOW)
    assert status(env, "log-rotation") == "provisional" and H.list_history(env.skills, "log-rotation") == []


def test_only_the_loops_own_skills_are_ever_touched(env):
    make(env, "owner-skill", origin="owner")
    make(env, "pinned-one")
    H.pin_skill(env.skills, "pinned-one")
    use(env, "owner-skill", ["tool_error"] * 4)
    use(env, "pinned-one", ["no_error_seen"] * 3)
    assert LC.evaluate_lifecycle(env.skills, env.db, now=NOW) == []
    assert "metadata" not in parse_frontmatter((env.skills / "owner-skill" / "SKILL.md").read_text(encoding="utf-8"))[0]
    assert status(env, "pinned-one") == "provisional"


def test_evaluating_twice_changes_nothing_the_second_time(env):
    make(env, "log-rotation")
    use(env, "log-rotation", ["no_error_seen"] * 3)
    assert len(LC.evaluate_lifecycle(env.skills, env.db, now=NOW)) == 1
    assert LC.evaluate_lifecycle(env.skills, env.db, now=NOW) == []


def test_a_skill_that_vanishes_or_has_no_skill_md_is_skipped_quietly(env):
    (env.skills / "empty-dir").mkdir()
    assert LC.evaluate_lifecycle(env.skills, env.db, now=NOW) == []
    assert LC.evaluate_lifecycle(env.tmp / "no-such-dir", env.db, now=NOW) == []


# ── the owner vouches ───────────────────────────────────────────────────────

def test_approve_establishes_an_agent_skill_whatever_its_record(env):
    make(env, "dns-checks", status="flagged")
    assert LC.approve_skill(env.skills, "dns-checks") == "established"
    assert status(env, "dns-checks") == "established"
    assert LC.approve_skill(env.skills, "dns-checks") == "already established"
    assert any(e["action"] == "approve" and e["actor"] == "owner" for e in H.read_log(env.skills, "dns-checks"))


def test_approve_refuses_the_owners_own_skills(env):
    make(env, "owner-skill", origin="owner")
    with pytest.raises(H.SkillHistoryError, match="yours"):
        LC.approve_skill(env.skills, "owner-skill")


# ── what the agent is told ──────────────────────────────────────────────────

@pytest.mark.parametrize("state,note_fragment,suffix", [
    ("provisional", "has not yet proven itself", " [provisional, agent-written]"),
    ("flagged", "recent uses have failed", " [flagged: recent uses failed]"),
    ("established", None, ""),
])
def test_the_agent_is_told_a_skills_standing(env, state, note_fragment, suffix):
    make(env, "dns-checks", status=state)
    note = LC.status_note(env.skills, "dns-checks")
    assert (note_fragment in note) if note_fragment else note == ""
    assert LC.catalogue_suffix(env.skills, "dns-checks") == suffix


def test_the_owners_skills_and_unknown_names_carry_no_note(env):
    make(env, "owner-skill", origin="owner")
    assert LC.status_note(env.skills, "owner-skill") == "" and LC.catalogue_suffix(env.skills, "owner-skill") == ""
    assert LC.status_note(env.skills, "../escape") == "" and LC.catalogue_suffix(env.skills, "../escape") == ""


def test_the_skill_tool_prepends_the_banner_and_the_catalogue_marks_provisional_skills(env, monkeypatch):
    from lisan.tools.execution_tools import agent_tools, skill_tool

    monkeypatch.setenv("LISAN_SKILLS_DIR", str(env.skills))
    make(env, "dns-checks", status="provisional")
    make(env, "owner-skill", origin="owner")
    body = skill_tool(name="dns-checks")
    assert body.startswith("[This skill was written by the agent") and "body" in body
    assert not skill_tool(name="owner-skill").startswith("[")
    skill_spec = next(t for t in agent_tools(env.skills) if t["name"] == "skill")["description"]
    assert "dns-checks: Use when testing. [provisional, agent-written]" in skill_spec
    assert "owner-skill: Use when owning." in skill_spec and "owner-skill: Use when owning. [" not in skill_spec


def test_revising_a_flagged_skill_puts_it_back_on_probation(env):
    from lisan.tools import skill_gate as G

    make(env, "dns-checks", status="flagged")
    events = {"e1": {"id": "e1", "conversation_id": "telegram-1", "sources": ["owner"], "tainted": False}}
    verdict = G.gate_operation(
        G.Operation(op="patch", skill="dns-checks", old_text="body", new_text="better body", evidence=["e1"], rationale="r"),
        skills_dir=env.skills, events=events, batch_ids={"e1"}, today="2026-10-02")
    assert verdict.accepted and parse_frontmatter(verdict.change.files["SKILL.md"])[0]["metadata"]["status"] == "provisional"
    make(env, "established-one", status="established")
    v2 = G.gate_operation(
        G.Operation(op="patch", skill="established-one", old_text="body", new_text="better body", evidence=["e1"], rationale="r"),
        skills_dir=env.skills, events=events, batch_ids={"e1"}, today="2026-10-02")
    assert parse_frontmatter(v2.change.files["SKILL.md"])[0]["metadata"]["status"] == "established"  # a proven skill stays proven
