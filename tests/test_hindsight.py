"""Hindsight elevation: significance only rises, only with later-dated
evidence, and the elevation carries its own provenance note."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.dreamer_ops import _apply_hindsight_elevations, _bundle_hindsight
from lisan.tools.record_factory import new_episode


class HindsightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "episodes").mkdir(parents=True)
        self.early = new_episode(self.vault, "First mention of the seed library",
                                 summary="Ruth mentioned wanting a community seed library.",
                                 significance="low").path
        self.late = new_episode(self.vault, "Seed library opens",
                                 summary="The seed library opened with forty members.",
                                 significance="high").path
        # Backdate the early episode so "later" is decidable.
        early_doc = load_markdown(self.early)
        fm = dict(early_doc.frontmatter)
        fm["created"] = "2026-06-01"
        from lisan.frontmatter import write_markdown

        write_markdown(self.early, fm, early_doc.body)
        self.early_id = fm["id"]
        self.late_id = load_markdown(self.late).frontmatter["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _elevate(self, **overrides):
        proposal = {
            "episode_id": self.early_id,
            "new_significance": "high",
            "reason": "the offhand mention became the year's project",
            "evidence_refs": [self.late_id],
        }
        proposal.update(overrides)
        return _apply_hindsight_elevations(self.vault, {"elevations": [proposal]})

    def test_bundle_lists_episodes_in_date_order(self) -> None:
        bundle = _bundle_hindsight(self.vault)
        self.assertLess(bundle.index(self.early_id), bundle.index(self.late_id))
        self.assertIn("significance: low", bundle)

    def test_valid_elevation_applies_with_provenance(self) -> None:
        applied = self._elevate()
        self.assertEqual(applied, self.early)
        doc = load_markdown(self.early)
        self.assertEqual(doc.frontmatter["significance"], "high")
        self.assertIn("## Hindsight", doc.body)
        self.assertIn(self.late_id, doc.body)

    def test_demotion_is_refused(self) -> None:
        self.assertIsNone(_apply_hindsight_elevations(self.vault, {"elevations": [{
            "episode_id": self.late_id, "new_significance": "low",
            "reason": "meh", "evidence_refs": [self.early_id]}]}))
        self.assertEqual(load_markdown(self.late).frontmatter["significance"], "high")

    def test_earlier_or_missing_evidence_is_refused(self) -> None:
        self.assertIsNone(self._elevate(evidence_refs=["episode.does-not-exist"]))
        # Evidence dated BEFORE the target is not hindsight either.
        self.assertIsNone(_apply_hindsight_elevations(self.vault, {"elevations": [{
            "episode_id": self.late_id, "new_significance": "high",
            "reason": "circular", "evidence_refs": [self.early_id]}]}))
        self.assertEqual(load_markdown(self.early).frontmatter["significance"], "low")

    def test_unknown_episode_and_empty_response_are_noops(self) -> None:
        self.assertIsNone(self._elevate(episode_id="episode.nope"))
        self.assertIsNone(_apply_hindsight_elevations(self.vault, {}))


if __name__ == "__main__":
    unittest.main()
