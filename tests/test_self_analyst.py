"""Tests for the agent self-analysis pass (WO-PSYCHE §4.4)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.analyst_ops import (
    AnalystRunResult,
    build_self_analyst_bundle,
    find_self_entity,
    run_self_analyst_scan,
    self_analysis_eligible,
    _MIN_SELF_EPISODES,
    _MIN_SELF_EVAL_ENTRIES,
    _SELF_EVAL_HISTORY_REL,
)
from lisan.tools.epistemic import PATTERN_TYPES, discover_self_pattern_hypotheses
from lisan.tools.job_policy import (
    COALESCE_AGGRESSIVE,
    DEFAULT_JOB_PRIORITIES,
    _should_queue_self_analyst,
)
from lisan.tools.jobs import JOB_TYPES


def _write_self_entity(vault: Path, name: str = "Testagent") -> Path:
    agents_dir = vault / "entities" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name.lower()}.md"
    fm = {
        "id": f"entity.agent.{name.lower()}",
        "type": "entity",
        "kind": "agent",
        "subtype": "agent",
        "canonical_name": name,
        "aliases": [name],
        "software": "Lisan",
    }
    write_markdown(path, fm, f"# {name}\n\nThe agent's own entity record.\n")
    return path


def _write_self_episodes(vault: Path, count: int = 5) -> list[Path]:
    episodes_dir = vault / "self" / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        d = (date.today() - timedelta(days=count - i)).isoformat()
        path = episodes_dir / f"{d}-event-{i}.md"
        fm = {
            "id": f"self_episode.event-{i}",
            "type": "self_episode",
            "created": d,
            "updated": d,
            "status": "active",
            "significance": "low",
            "domain_primary": "cross_arena",
            "domain_secondary": [],
            "privacy": "personal",
            "disclosure": "private",
            "summary": f"I delivered reminder {i} and it succeeded.",
            "title": f"Task delivery {i}",
            "event_kind": "task",
            "source_refs": [f"jobs:{42 + i}"],
            "outcome": "succeeded" if i % 3 != 0 else "failed",
        }
        body = f"# Task delivery {i}\n\n## What happened\n\nI delivered reminder {i}.\n"
        write_markdown(path, fm, body)
        paths.append(path)
    return paths


def _write_self_eval_history(vault: Path, count: int = 3) -> Path:
    path = vault / _SELF_EVAL_HISTORY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(count):
        d = (date.today() - timedelta(days=count - i)).isoformat()
        entry = {
            "date": d,
            "exchanges": 20 + i,
            "judged": 15 + i,
            "dimensions": {
                "continuity": {"mean": 4.0 + (i * 0.1), "n": 10},
                "initiative": {"mean": 3.5 - (i * 0.1), "n": 10},
            },
            "overall_mean": 3.75,
            "health": {
                "capture_failure_rate": 0.02,
                "empty_responses": 1,
                "failed_turns": 0,
                "records_created": {"entities": 2, "episodes": 3, "knowledge": 1},
            },
        }
        lines.append(json.dumps(entry, ensure_ascii=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestFindSelfEntity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_entity_by_software_field(self):
        _write_self_entity(self.vault)
        result = find_self_entity(self.vault)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "entity.agent.testagent")
        self.assertEqual(result["canonical_name"], "Testagent")

    def test_returns_none_when_no_agent_entity(self):
        result = find_self_entity(self.vault)
        self.assertIsNone(result)

    def test_ignores_non_lisan_agents(self):
        agents_dir = self.vault / "entities" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / "other-bot.md"
        fm = {
            "id": "entity.agent.other-bot",
            "type": "entity",
            "kind": "agent",
            "subtype": "agent",
            "canonical_name": "OtherBot",
            "software": "SomethingElse",
        }
        write_markdown(path, fm, "# OtherBot\n")
        result = find_self_entity(self.vault)
        self.assertIsNone(result)


class TestSelfAnalysisEligibility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        _write_self_entity(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_eligible_with_no_evidence(self):
        result = self_analysis_eligible(self.vault)
        self.assertIsNone(result)

    def test_eligible_with_enough_episodes(self):
        _write_self_episodes(self.vault, count=_MIN_SELF_EPISODES)
        result = self_analysis_eligible(self.vault)
        self.assertIsNotNone(result)
        self.assertEqual(result["episode_count"], _MIN_SELF_EPISODES)

    def test_eligible_with_enough_eval_history(self):
        _write_self_eval_history(self.vault, count=_MIN_SELF_EVAL_ENTRIES)
        result = self_analysis_eligible(self.vault)
        self.assertIsNotNone(result)
        self.assertEqual(result["eval_count"], _MIN_SELF_EVAL_ENTRIES)

    def test_not_eligible_below_both_thresholds(self):
        _write_self_episodes(self.vault, count=_MIN_SELF_EPISODES - 1)
        _write_self_eval_history(self.vault, count=_MIN_SELF_EVAL_ENTRIES - 1)
        result = self_analysis_eligible(self.vault)
        self.assertIsNone(result)


class TestBuildSelfAnalystBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        _write_self_entity(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundle_includes_self_episodes(self):
        _write_self_episodes(self.vault, count=3)
        bundle = build_self_analyst_bundle(self.vault)
        self.assertIn("## Self-Episodes", bundle)
        self.assertIn("Task delivery", bundle)

    def test_bundle_includes_eval_history(self):
        _write_self_eval_history(self.vault, count=3)
        bundle = build_self_analyst_bundle(self.vault)
        self.assertIn("## Self-Evaluation History", bundle)
        self.assertIn("continuity", bundle)
        self.assertIn("initiative", bundle)

    def test_bundle_includes_subject_identity(self):
        bundle = build_self_analyst_bundle(self.vault)
        self.assertIn("## Analysis Subject", bundle)
        self.assertIn("Testagent", bundle)
        self.assertIn("kind: agent (self)", bundle)

    def test_bundle_omits_person_checkins(self):
        people_dir = self.vault / "entities" / "people"
        people_dir.mkdir(parents=True, exist_ok=True)
        person_path = people_dir / "person-42.md"
        fm = {
            "id": "entity.person.42",
            "type": "entity",
            "kind": "person",
            "subtype": "person",
            "canonical_name": "Person42",
        }
        write_markdown(person_path, fm, "# Person42\n")
        bundle = build_self_analyst_bundle(self.vault)
        self.assertNotIn("Person42", bundle)

    def test_bundle_includes_active_deviation_loops(self):
        loops_dir = self.vault / "open_loops"
        loops_dir.mkdir(parents=True, exist_ok=True)
        loop_path = loops_dir / "2026-08-01-deviation-interocept-failed-jobs.md"
        fm = {
            "id": "open_loop.deviation-interocept-failed-jobs",
            "type": "open_loop",
            "origin": "self",
            "deviation_fingerprint": "interocept-failed-jobs",
            "deviation_class": "interocept",
            "status": "active",
            "summary": "3 failed jobs in the last 24 hours",
        }
        write_markdown(loop_path, fm, "# Deviation: interocept-failed-jobs\n")
        bundle = build_self_analyst_bundle(self.vault)
        self.assertIn("## Active Deviation Loops", bundle)
        self.assertIn("interocept-failed-jobs", bundle)


class TestRunSelfAnalystScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        _write_self_entity(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ineligible_returns_empty_result(self):
        result = run_self_analyst_scan(vault=self.vault, db_path=self.db_path)
        self.assertIsInstance(result, AnalystRunResult)
        self.assertEqual(result.pattern_paths, [])
        self.assertIn("not yet eligible", result.response["summary"])

    def test_eligible_scan_runs_deterministic_fallback(self):
        _write_self_episodes(self.vault, count=10)
        _write_self_eval_history(self.vault, count=5)
        result = run_self_analyst_scan(vault=self.vault, db_path=self.db_path)
        self.assertIsInstance(result, AnalystRunResult)
        self.assertIn("summary", result.response)
        report_path = result.report_path
        self.assertTrue(report_path.exists())
        fm = load_markdown(report_path).frontmatter
        self.assertEqual(fm["task"], "self_analyst")

    def test_report_links_patterns_and_reviews(self):
        _write_self_episodes(self.vault, count=10)
        _write_self_eval_history(self.vault, count=5)
        result = run_self_analyst_scan(vault=self.vault, db_path=self.db_path)
        fm = load_markdown(result.report_path).frontmatter
        expected_links = len(result.pattern_paths) + len(result.review_paths)
        self.assertEqual(len(fm.get("links") or []), expected_links)

    def test_patterns_linked_to_self_entity(self):
        episodes = _write_self_episodes(self.vault, count=10)
        for ep in episodes:
            doc = load_markdown(ep)
            updated = dict(doc.frontmatter)
            updated["summary"] = "I failed to deliver a reminder because the service timed out. Error and retry."
            write_markdown(ep, updated, doc.body)
        result = run_self_analyst_scan(vault=self.vault, db_path=self.db_path)
        for pattern_path in result.pattern_paths:
            fm = load_markdown(pattern_path).frontmatter
            links = fm.get("links") or []
            self.assertIn("entity.agent.testagent", links)


class TestDiscoverSelfPatternHypotheses(unittest.TestCase):
    def test_finds_failure_clustering(self):
        bundle = (
            "### self/episodes/2026-08-01-event-1.md\n"
            "id: self_episode.event-1\n"
            "I failed to deliver the reminder. Error: timeout. Retry attempted.\n\n"
            "### self/episodes/2026-08-02-event-2.md\n"
            "id: self_episode.event-2\n"
            "I failed again. Error: service unavailable. Retry succeeded.\n\n"
            "### self/episodes/2026-08-03-event-3.md\n"
            "id: self_episode.event-3\n"
            "Another failed delivery with an exception trace.\n"
        )
        patterns = discover_self_pattern_hypotheses(bundle)
        types = [p["pattern_type"] for p in patterns]
        self.assertIn("failure_clustering", types)

    def test_returns_empty_on_clean_history(self):
        bundle = (
            "### self/episodes/2026-08-01-event-1.md\n"
            "id: self_episode.event-1\n"
            "Delivered reminder successfully.\n\n"
            "### self/episodes/2026-08-02-event-2.md\n"
            "id: self_episode.event-2\n"
            "Delivered task on time.\n"
        )
        patterns = discover_self_pattern_hypotheses(bundle)
        self.assertEqual(patterns, [])


class TestSelfAnalystPatternTypes(unittest.TestCase):
    def test_new_pattern_types_are_registered(self):
        for pt in [
            "quality_regression",
            "failure_clustering",
            "recovery_pattern",
            "scope_creep",
            "explanation_invention",
            "execution_gap",
        ]:
            self.assertIn(pt, PATTERN_TYPES, f"{pt} missing from PATTERN_TYPES")


class TestJobRegistration(unittest.TestCase):
    def test_self_scan_is_registered_job_type(self):
        self.assertIn("analyst.self_scan", JOB_TYPES)

    def test_self_scan_has_priority(self):
        self.assertIn("analyst.self_scan", DEFAULT_JOB_PRIORITIES)

    def test_self_scan_coalesces_aggressively(self):
        self.assertIn("analyst.self_scan", COALESCE_AGGRESSIVE)


class TestSelfAnalystScheduling(unittest.TestCase):
    def test_queues_when_never_run(self):
        self.assertTrue(_should_queue_self_analyst(None))
