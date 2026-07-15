"""Ship 1 of WO-PSYCHE: check-in capture and the support layer.

The load-bearing contracts: captures are OBSERVATIONAL (the tool
description forbids interpretation — pinned here), unknown subjects are
refused rather than minted, context tags ride as deterministic
``context:`` observed-facts entries for the future analyst, and support
strategies accumulate a dated track record on schema-valid
``support_strategy`` pattern records."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.checkin import record_checkin, support_note, support_summary


def _make_vault() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    ensure_repo_layout(root)
    return tmp, vault_root(root)


def _seed_person(vault: Path, slug: str, canonical: str) -> Path:
    path = vault / "entities" / "people" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": f"entity.{slug}", "type": "entity", "subtype": "person", "kind": "person",
        "canonical_name": canonical, "aliases": [], "summary": f"{canonical} is a person.",
        "significance": "medium", "confidence": "low", "confidence_basis": "seed",
        "created": "2026-07-01", "updated": "2026-07-01", "status": "active",
        "domain_primary": "relational", "domain_secondary": [], "privacy": "personal",
        "disclosure": "private", "review_after": "2026-07-01", "last_confirmed": "2026-07-01",
        "epoch": 1, "epoch_started": "2026-07-01", "previous_epochs": [], "links": [],
    }
    path.write_text(dump_markdown(fm, f"# {canonical}\n\n{canonical} is a person.\n"), encoding="utf-8")
    return path


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.vault = _make_vault()
        _seed_person(self.vault, "maya", "Maya")

    def tearDown(self):
        self.tmp.cleanup()

    def test_checkin_creates_linked_evidence_with_context_tags(self):
        out = record_checkin(
            self.vault, "maya", "quiet after school, went straight to her room",
            tags=["school-day", "transition-evening"], quote="I'm fine.",
        )
        self.assertTrue(out["ok"])
        doc = load_markdown(Path(out["path"]))
        fm = doc.frontmatter
        self.assertEqual(fm["type"], "evidence")
        self.assertEqual(fm["source_type"], "checkin")
        self.assertIn("entity.maya", fm["links"])
        self.assertIn("Maya", fm["actors"])
        self.assertIn("context: school-day", fm["observed_facts"])
        self.assertIn("context: transition-evening", fm["observed_facts"])
        self.assertEqual(fm["verbatim_excerpt"], "I'm fine.")
        self.assertEqual(fm["disclosure"], "private")
        # precise time captured, not just the date
        self.assertIn("T", str(fm.get("timestamp_of_artifact")))

    def test_unknown_subject_is_refused_not_minted(self):
        out = record_checkin(self.vault, "somebody-new", "seemed tired")
        self.assertFalse(out["ok"])
        self.assertIn("Maya", out["known_people"])
        self.assertEqual(list((self.vault / "evidence" / "records").glob("*checkin*")), [])

    def test_checkin_record_validates(self):
        # This test previously read `report.errors`, an attribute that does
        # not exist (the report holds `issues`), so it passed vacuously while
        # every real check-in failed validation on source_type "checkin".
        from lisan.tools.validator import validate_vault

        record_checkin(self.vault, "maya", "smiled at drop-off", tags=["school-day"])
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.vault = _make_vault()
        _seed_person(self.vault, "maya", "Maya")

    def tearDown(self):
        self.tmp.cleanup()

    def test_outcomes_accumulate_on_one_pattern(self):
        first = support_note(self.vault, "maya", "the bubble game", "worked", note="calm in 2 min")
        self.assertTrue(first["ok"])
        second = support_note(self.vault, "maya", "The Bubble Game", "didnt_work", note="too wound up")
        self.assertTrue(second["ok"])
        self.assertEqual(first["path"], second["path"], "same strategy must land on one record")

        fm = load_markdown(Path(first["path"])).frontmatter
        self.assertEqual(fm["pattern_type"], "support_strategy")
        self.assertIn("entity.maya", fm["links"])
        self.assertEqual(len(fm["supporting_records"]), 1)
        self.assertEqual(len(fm["counterexamples"]), 1)
        self.assertIn("didnt_work", fm["counterexamples"][0])
        self.assertNotIn("No explicit counterexamples", str(fm["counterexamples"]))

    def test_summary_lists_what_helps(self):
        support_note(self.vault, "maya", "feelings dichotomies", "worked")
        support_note(self.vault, "maya", "countdown timer", "mixed")
        out = support_summary(self.vault, "maya")
        self.assertTrue(out["ok"])
        names = " ".join(s["strategy"] for s in out["strategies"])
        self.assertIn("feelings dichotomies", names)
        self.assertIn("countdown timer", names)

    def test_bad_outcome_is_refused(self):
        out = support_note(self.vault, "maya", "x", "sorta")
        self.assertFalse(out["ok"])

    def test_support_pattern_validates(self):
        from lisan.tools.validator import validate_vault

        support_note(self.vault, "maya", "the bubble game", "worked")
        report = validate_vault(self.vault)
        errors = [e for e in getattr(report, "errors", []) if "support" in str(e).lower()]
        self.assertEqual(errors, [])


class ToolWiringTests(unittest.TestCase):
    def test_tools_registered_with_neutrality_rule(self):
        from lisan.tools.execution_tools import TOOLS

        by_name = {t["name"]: t for t in TOOLS}
        self.assertIn("checkin", by_name)
        self.assertIn("support_note", by_name)
        # The neutrality rule lives in the tool description the model reads
        # every turn — pin it (WO-PSYCHE Ship 1 definition of done).
        desc = by_name["checkin"]["description"]
        self.assertIn("NEVER interpretation", desc)
        self.assertIn("person", by_name["checkin"]["parameters"]["required"][0])

    def test_handlers_round_trip(self):
        from lisan.tools.execution_tools import build_tool_handlers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            _seed_person(vault, "maya", "Maya")
            handlers = build_tool_handlers(vault=vault, db_path=root / "jobs.sqlite", config={})
            out = json.loads(handlers["checkin"]("maya", "calm evening", ["home-day"], None))
            self.assertTrue(out["ok"])
            out = json.loads(handlers["support_note"]("maya", "quiet corner", "worked", "self-initiated"))
            self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
