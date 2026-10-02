"""Learning loop step 4: skill outcomes become self-knowledge.

A skill's record feeds three things the system already has: first-person episodes
(the Self-Analyst's and the belief extractor's raw material), capability beliefs
(deterministic, owner-ratified), and drives (a failing procedure aches until it
is fixed). Nothing here invents a mechanism; each plugs into the existing one.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lisan.frontmatter import load_markdown
from lisan.tools import belief_formation as BF
from lisan.tools import learning as L
from lisan.tools import self_episodes as SE
from lisan.tools.deviations import detect, scan_deviations


@pytest.fixture()
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "self" / "episodes").mkdir(parents=True)
    (vault / "open_loops").mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("LISAN_SKILLS_DIR", str(skills))
    return SimpleNamespace(vault=vault, db=tmp_path / "learn.sqlite", skills=skills, tmp=tmp_path)


def make_skill(env, name, *, origin="agent", status=None):
    (env.skills / name).mkdir(exist_ok=True)
    lines = (["  origin: agent"] if origin == "agent" else []) + ([f"  status: {status}"] if status else [])
    meta = ("metadata:\n" + "\n".join(lines) + "\n") if lines else ""  # an owner skill can carry a status too
    (env.skills / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when testing {name}.\nversion: 1.0.0\n{meta}---\n\nbody\n", encoding="utf-8")


def use(env, skill, outcome, day, n=1, event_id=None):
    conn = L._connect(env.db)
    for i in range(n):
        conn.execute("INSERT INTO skill_usage (skill, skill_kind, event_id, used_at, outcome) VALUES (?,?,?,?,?)",
                     (skill, "instructional", event_id or f"turn:job.{skill}.{day}.{i}.{outcome}", f"{day}T10:00:00Z", outcome))
    conn.commit()
    conn.close()


# ── skill use as first-person episodes ──────────────────────────────────────

def test_a_skill_use_becomes_an_honest_first_person_episode(env):
    use(env, "gmail_search", "no_error_seen", "2026-08-20")
    use(env, "gmail_search", "tool_error", "2026-08-21")
    ok, bad = sorted(SE.skill_events(env.db), key=lambda e: e.date)
    assert (ok.event_kind, ok.outcome, ok.skill, ok.date) == ("skill", "succeeded", "gmail_search", "2026-08-20")
    assert "ran without an error" in ok.narration and "worked" not in ok.narration  # not claiming more than is known
    assert (bad.outcome, bad.significance) == ("failed", "medium") and "hit an error" in bad.narration
    assert ok.source_refs == ["learning:turn:job.gmail_search.2026-08-20.0.no_error_seen"]
    assert "{{self}}" in ok.narration and "{{principal}}" in ok.narration  # told in the agent's own voice


def test_an_outcome_nobody_can_judge_is_not_an_episode(env):
    use(env, "gmail_search", "unknown", "2026-08-20")
    use(env, "gmail_search", "no_error_seen", "2026-08-21")
    assert [e.outcome for e in SE.skill_events(env.db)] == ["succeeded"]


def test_a_use_inside_a_plan_cites_its_job_because_it_has_no_event(env):
    use(env, "research", "no_error_seen", "2026-08-20", event_id="plan-turn:job.abc123")
    (event,) = SE.skill_events(env.db)
    assert event.source_refs == ["jobs:job.abc123"]


def test_episode_ids_are_stable_and_distinct_per_use(env):
    use(env, "research", "no_error_seen", "2026-08-20", n=3)
    first = [e.event_id for e in SE.skill_events(env.db)]
    assert len(set(first)) == 3 and first == [e.event_id for e in SE.skill_events(env.db)]


def test_no_ledger_yet_means_no_skill_episodes_not_an_error(env):
    assert SE.skill_events(env.tmp / "never-created.sqlite") == []
    sqlite3.connect(env.tmp / "empty.sqlite").close()
    assert SE.skill_events(env.tmp / "empty.sqlite") == []


def test_the_episode_file_records_which_skill(env):
    use(env, "gmail_search", "no_error_seen", "2026-08-20")
    (event,) = SE.skill_events(env.db)
    path = SE.write_self_episode(env.vault, event, env.db)
    fm = dict(load_markdown(path).frontmatter)
    assert fm["event_kind"] == "skill" and fm["skill"] == "gmail_search" and fm["outcome"] == "succeeded"
    assert fm["source_refs"] and fm["type"] == "self_episode"
    assert SE.write_self_episode(env.vault, event, env.db) is None  # idempotent
    schema = json.loads((Path(__file__).resolve().parents[1] / "lisan" / "schemas" / "self_episode.schema.json").read_text())
    assert "skill" in schema["properties"]["event_kind"]["enum"] and "skill" in schema["properties"]


def test_recording_a_turn_that_used_a_skill_writes_its_episode_immediately(env):
    (env.skills / "gmail_search").mkdir()
    (env.skills / "gmail_search" / "SKILL.md").write_text("---\nname: gmail_search\ndescription: d\n---\nb\n", encoding="utf-8")
    payload = {"text": "find it", "response": "ok", "conversation_id": "telegram-1",
               "tool_calls": [{"tool": "skill", "args": {"name": "gmail_search"}, "result": "x"}]}
    L.record_turn_event(payload, job_id="job.live", vault=env.vault, db_path=env.db, skills_dir=env.skills)
    episodes = list((env.vault / "self" / "episodes").glob("*.md"))
    assert len(episodes) == 1 and dict(load_markdown(episodes[0]).frontmatter)["skill"] == "gmail_search"


def test_a_skill_used_inside_a_plan_also_becomes_an_episode(env):
    payload = {"text": "t", "response": "r", "conversation_id": "plan-plan.x",
               "tool_calls": [{"tool": "skill", "args": {"name": "research"}, "result": "x"}]}
    L.record_turn_event(payload, job_id="job.inplan", vault=env.vault, db_path=env.db, skills_dir=env.skills)
    (path,) = (env.vault / "self" / "episodes").glob("*.md")
    assert dict(load_markdown(path).frontmatter)["source_refs"] == ["jobs:job.inplan"]


def test_a_failing_episode_writer_cannot_harm_the_recording(env):
    payload = {"text": "t", "response": "r", "conversation_id": "telegram-1",
               "tool_calls": [{"tool": "skill", "args": {"name": "research"}, "result": "x"}]}
    with patch("lisan.tools.self_episodes.record_skill_episodes", side_effect=RuntimeError("disk full")):
        assert L.record_turn_event(payload, job_id="job.safe", vault=env.vault, db_path=env.db, skills_dir=env.skills)
    assert L.list_events(db_path=env.db)  # the event was still recorded


def test_the_catch_up_pass_assembles_skill_episodes_from_history(env):
    use(env, "gmail_search", "no_error_seen", "2026-08-20", n=2)
    result = SE.assemble_self_episodes(env.vault, env.db)
    assert result["written"] == 2
    assert SE.assemble_self_episodes(env.vault, env.db)["written"] == 0


# ── capability beliefs ──────────────────────────────────────────────────────

def seed_episodes(env, skill, outcomes_by_day):
    for day, outcome, n in outcomes_by_day:
        use(env, skill, outcome, day, n=n)
    SE.assemble_self_episodes(env.vault, env.db)


def statements(env):
    return {c.statement: c for c in BF.extract_belief_candidates(env.vault)}


def test_a_skill_that_has_held_up_across_days_becomes_a_belief_candidate_with_its_evidence(env):
    seed_episodes(env, "gmail_search", [("2026-08-20", "no_error_seen", 2), ("2026-08-22", "no_error_seen", 2)])
    candidate = statements(env)["My gmail_search skill has held up in use."]
    assert len(candidate.supporting) == 4 and candidate.days == {"2026-08-20", "2026-08-22"}
    assert candidate.counterexamples == [] and all(i.startswith("self_episode.skill-gmail-search-") for i in candidate.supporting)


def test_the_gate_is_unchanged_three_uses_on_two_days_and_low_contradiction(env):
    seed_episodes(env, "a_skill", [("2026-08-20", "no_error_seen", 2)])  # too few, one day
    seed_episodes(env, "b_skill", [("2026-08-20", "no_error_seen", 3)])  # enough, but one day
    seed_episodes(env, "c_skill", [("2026-08-20", "no_error_seen", 2), ("2026-08-21", "no_error_seen", 1), ("2026-08-22", "tool_error", 2)])  # contradicted
    assert statements(env) == {}


def test_a_skill_that_keeps_failing_forms_the_opposite_candidate_not_the_reliable_one(env):
    seed_episodes(env, "gmail_search", [("2026-08-20", "tool_error", 2), ("2026-08-22", "tool_error", 2), ("2026-08-23", "no_error_seen", 1)])
    found = statements(env)
    assert "My gmail_search skill fails often enough that I should double-check what it returns." in found
    assert "My gmail_search skill has held up in use." not in found
    assert len(found["My gmail_search skill fails often enough that I should double-check what it returns."].counterexamples) == 1  # listed, not hidden


def test_each_skill_is_judged_on_its_own_record(env):
    seed_episodes(env, "gmail_search", [("2026-08-20", "no_error_seen", 2), ("2026-08-21", "no_error_seen", 2)])
    seed_episodes(env, "youtube_transcript", [("2026-08-20", "tool_error", 2), ("2026-08-21", "tool_error", 2)])
    found = statements(env)
    assert set(found) == {"My gmail_search skill has held up in use.",
                          "My youtube_transcript skill fails often enough that I should double-check what it returns."}


def test_the_candidates_are_deterministic_and_the_statement_names_no_counts(env):
    seed_episodes(env, "gmail_search", [("2026-08-20", "no_error_seen", 2), ("2026-08-21", "no_error_seen", 2)])
    first = [c.statement for c in BF.extract_belief_candidates(env.vault)]
    use(env, "gmail_search", "no_error_seen", "2026-08-25", n=3)  # more evidence: the same belief, not a new one
    SE.assemble_self_episodes(env.vault, env.db)
    assert [c.statement for c in BF.extract_belief_candidates(env.vault)] == first


def test_ratification_re_verifies_skill_evidence_and_is_idempotent(env):
    seed_episodes(env, "gmail_search", [("2026-08-20", "no_error_seen", 2), ("2026-08-21", "no_error_seen", 2)])
    artifact = Path(BF.run_belief_extraction(env.vault)["artifact"])
    created = BF.ratify_beliefs(env.vault, artifact_path=artifact)
    assert len(created) == 1 and "gmail_search skill has held up" in created[0].read_text(encoding="utf-8")
    assert BF.ratify_beliefs(env.vault, artifact_path=artifact) == []  # ratifying twice forms nothing new


def test_skill_beliefs_obey_the_candidate_cap(env):
    for i in range(9):  # more skills than the cap allows
        seed_episodes(env, f"skill_{i:02d}", [("2026-08-20", "no_error_seen", 2), ("2026-08-21", "no_error_seen", 2)])
    assert len(BF.extract_belief_candidates(env.vault)) == BF.MAX_CANDIDATES


# ── drives: a failing procedure aches ───────────────────────────────────────

CFG = {"deviations": {"daily_cap": 5}}


def skill_health(env, **cfg):
    return [d for d in detect(env.vault, db_path=env.db, config={"deviations": cfg}) if d["klass"] == "skill_health"]


def test_a_skill_that_fails_most_of_the_time_aches(env):
    make_skill(env, "gmail_search", origin="owner")
    today = date.today().isoformat()
    use(env, "gmail_search", "tool_error", today, n=3)
    use(env, "gmail_search", "no_error_seen", today, n=2)
    (found,) = skill_health(env)
    assert found["fingerprint"] == "skill-unreliable-gmail-search"
    assert found["summary"] == "my gmail_search skill failed on 3 of its last 5 uses — I cannot rely on it"
    assert found["links"] == []


@pytest.mark.parametrize("failed,ok", [(2, 3), (4, 0), (1, 1)])
def test_below_the_thresholds_a_skill_does_not_ache(env, failed, ok):
    """Too few uses (4 of 4 failing) or a minority of failures (2 of 5) is not enough."""
    today = date.today().isoformat()
    use(env, "gmail_search", "tool_error", today, n=failed)
    use(env, "gmail_search", "no_error_seen", today, n=ok)
    assert skill_health(env) == []


def test_old_failures_outside_the_window_do_not_ache(env):
    use(env, "gmail_search", "tool_error", "2025-01-01", n=6)
    assert skill_health(env) == []


def test_a_flagged_agent_written_skill_aches_but_an_owner_skill_marked_flagged_does_not(env):
    make_skill(env, "log-rotation", origin="agent", status="flagged")
    make_skill(env, "owners-own", origin="owner", status="flagged")
    found = skill_health(env)
    assert [d["fingerprint"] for d in found] == ["skill-flagged-log-rotation"]
    assert "I wrote my log-rotation skill from past work" in found[0]["summary"]


def test_the_ache_becomes_a_first_person_self_loop_and_closes_when_the_skill_is_fixed(env):
    make_skill(env, "log-rotation", origin="agent", status="flagged")
    summary = scan_deviations(env.vault, db_path=env.db, config=CFG)
    assert summary["emitted"] == 1
    (loop,) = (env.vault / "open_loops").glob("*skill-flagged-log-rotation.md")
    fm = dict(load_markdown(loop).frontmatter)
    assert fm["origin"] == "self" and fm["status"] == "active" and fm["deviation_class"] == "skill_health"
    assert "I wrote my log-rotation skill" in fm["summary"] or "log-rotation" in fm["summary"]
    # a revision puts the skill back on probation: the ache is gone, and so is the loop
    make_skill(env, "log-rotation", origin="agent", status="provisional")
    assert scan_deviations(env.vault, db_path=env.db, config=CFG)["satiated"] == 1
    assert dict(load_markdown(loop).frontmatter)["status"] == "resolved"


def test_an_unreliable_skill_is_not_refiled_while_the_loop_is_open(env):
    today = date.today().isoformat()
    use(env, "gmail_search", "tool_error", today, n=5)
    assert scan_deviations(env.vault, db_path=env.db, config=CFG)["emitted"] == 1
    assert scan_deviations(env.vault, db_path=env.db, config=CFG)["emitted"] == 0


def test_a_failing_skill_is_never_sent_to_the_code_self_repair_loop(env):
    """The remedy for a failing procedure is a revised skill, not a patch to the code."""
    make_skill(env, "log-rotation", origin="agent", status="flagged")
    enabled = {"deviations": {"daily_cap": 5}, "self_repair": {"targeted_command": ["pytest", "-q"]}}
    with patch("lisan.tools.action_policy.action_allowed", return_value=True), \
            patch("lisan.tools.jobs.enqueue_job") as enqueue:
        scan_deviations(env.vault, db_path=env.db, config=enabled)
    assert not [c for c in enqueue.call_args_list if c.args and c.args[0] == "self_repair.propose"]


def test_skill_aches_rank_after_the_machines_own_condition_and_before_housekeeping(env):
    from lisan.tools.deviations import _CLASS_ORDER

    assert _CLASS_ORDER.index("interocept") < _CLASS_ORDER.index("skill_health") < _CLASS_ORDER.index("thin")


def test_no_ledger_or_no_skills_directory_means_no_ache_not_an_error(env):
    assert skill_health(env) == []
    import shutil

    shutil.rmtree(env.skills)
    assert skill_health(env) == []


# ── the test process cannot see the owner's real skills ─────────────────────

def test_skills_root_is_contained_in_a_test_process(monkeypatch):
    from lisan import paths

    monkeypatch.delenv("LISAN_SKILLS_DIR", raising=False)
    resolved = paths.skills_root()
    assert resolved != Path.home() / ".local" / "share" / "Lisan" / "skills"
    monkeypatch.setenv("LISAN_ALLOW_TEST_SKILLS", "1")
    assert paths.skills_root() == Path.home() / ".local" / "share" / "Lisan" / "skills"
    monkeypatch.setenv("LISAN_SKILLS_DIR", "/tmp/explicit-skills")
    assert paths.skills_root() == Path("/tmp/explicit-skills")


# ── a quarantine is respected by the catch-up pass ──────────────────────────

def test_the_catch_up_pass_never_recreates_an_episode_the_owner_quarantined(env):
    """The first live run of the catch-up pass regenerated 149 episodes from the
    2026-07-27 plan-recursion incident that had been quarantined on purpose."""
    use(env, "gmail_search", "no_error_seen", "2026-08-20")
    use(env, "gmail_search", "no_error_seen", "2026-08-21")
    first, second = sorted(SE.skill_events(env.db), key=lambda e: e.date)
    quarantine = env.tmp / "quarantine-2026-07-27-plan-recursion" / "self-episodes"
    quarantine.mkdir(parents=True)
    (quarantine / SE.episode_path(env.vault, first).name).write_text("set aside by the owner", encoding="utf-8")
    result = SE.assemble_self_episodes(env.vault, env.db)
    written = {Path(p).name for p in result["paths"]}
    assert written == {SE.episode_path(env.vault, second).name}
    assert not SE.episode_path(env.vault, first).exists()
    assert SE.write_self_episode(env.vault, first, env.db) is None  # not by any other route either


def test_a_quarantine_folder_without_that_episode_blocks_nothing(env):
    use(env, "gmail_search", "no_error_seen", "2026-08-20")
    (env.tmp / "quarantine-other" / "self-episodes").mkdir(parents=True)
    (env.tmp / "quarantine-other" / "reports").mkdir()
    assert SE.assemble_self_episodes(env.vault, env.db)["written"] == 1
