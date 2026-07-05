"""WO-10: belief formation — deterministic candidates, hard evidence gate,
eval-history exclusion, counterexamples listed not hidden, owner-only
ratification with re-verification and birth-confidence cap."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.belief_formation import (
    extract_belief_candidates,
    ratify_beliefs,
    run_belief_extraction,
)
from lisan.tools.self_episodes import SelfEvent, write_self_episode


def _episode(vault: Path, i: int, *, kind: str = "plan", outcome: str = "succeeded",
             date: str = "2026-07-01", source_refs: list[str] | None = None,
             summary: str | None = None) -> None:
    write_self_episode(
        vault,
        SelfEvent(
            event_id=f"e{i}",
            event_kind=kind,
            date=date,
            title=summary or f"{kind} {i}",
            narration="{{self}} did a thing.",
            outcome=outcome,
            source_refs=source_refs if source_refs is not None else [f"jobs:{i}"],
        ),
    )


class ExtractionTests(unittest.TestCase):
    def test_generalization_over_outcomes_clears_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for i, day in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
                _episode(vault, i, date=day)
            candidates = extract_belief_candidates(vault)
            self.assertEqual(len(candidates), 1)
            self.assertIn("multi-step plans reliably", candidates[0].statement)
            self.assertEqual(len(candidates[0].supporting), 3)

    def test_single_day_support_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for i in range(4):
                _episode(vault, i, date="2026-07-01")  # plenty of support, one day
            self.assertEqual(extract_belief_candidates(vault), [])

    def test_contradiction_ratio_drops_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for i, day in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
                _episode(vault, i, date=day)
            for i in range(3, 5):  # 2 failures vs 3 successes → ratio 0.4 > 1/3
                _episode(vault, i, outcome="failed", date="2026-07-02")
            statements = [c.statement for c in extract_belief_candidates(vault)]
            self.assertFalse(any("reliably" in s for s in statements))

    def test_counterexamples_are_listed_when_within_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            for i, day in enumerate(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]):
                _episode(vault, i, date=day)
            _episode(vault, 9, outcome="failed", date="2026-07-02")  # 1/5 = 0.2 ok
            cand = extract_belief_candidates(vault)[0]
            self.assertEqual(len(cand.counterexamples), 1)

    def test_eval_tagged_and_sourceless_episodes_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _episode(vault, 0, date="2026-07-01")
            _episode(vault, 1, date="2026-07-02")
            # These two would clear the gate if counted:
            _episode(vault, 2, date="2026-07-03", summary="eval-run task cap-probe-1")
            _episode(vault, 3, date="2026-07-04", source_refs=[])
            self.assertEqual(extract_belief_candidates(vault), [])


class RatificationTests(unittest.TestCase):
    def _vault_with_artifact(self, tmp: str) -> tuple[Path, Path]:
        vault = Path(tmp)
        for i, day in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
            _episode(vault, i, date=day)
        result = run_belief_extraction(vault)
        return vault, Path(result["artifact"])

    def test_ratification_forms_beliefs_with_capped_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, artifact = self._vault_with_artifact(tmp)
            created = ratify_beliefs(vault, artifact_path=artifact)
            self.assertEqual(len(created), 1)
            fm = load_markdown(created[0]).frontmatter
            self.assertEqual(fm["type"], "self_belief")
            self.assertEqual(fm["belief_confidence"], "medium")  # never higher at birth
            self.assertEqual(len(fm["evidence_refs"]), 3)
            self.assertIn("Owner-ratified", fm["confidence_basis"])

    def test_ratification_reverifies_against_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, artifact = self._vault_with_artifact(tmp)
            # The artifact is a proposal, not an authority: delete the
            # evidence and ratification must refuse.
            for path in (vault / "self" / "episodes").glob("*.md"):
                path.unlink()
            self.assertEqual(ratify_beliefs(vault, artifact_path=artifact), [])

    def test_ratification_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault, artifact = self._vault_with_artifact(tmp)
            first = ratify_beliefs(vault, artifact_path=artifact)
            second = ratify_beliefs(vault, artifact_path=artifact)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_empty_artifact_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = run_belief_extraction(vault)  # no episodes at all
            self.assertEqual(result["candidates"], 0)
            with self.assertRaises(ValueError):
                ratify_beliefs(vault, artifact_path=Path(result["artifact"]))


if __name__ == "__main__":
    unittest.main()
