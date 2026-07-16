"""Ship 2 of WO-PSYCHE: the prediction ledger.

Contracts pinned here:
- a prediction cannot exist without a resolvable source (attribution is
  structural, not aspirational) or with clinical-label language;
- scoring is idempotent, evidence-gated, and bounded (a hit/miss citing
  records outside the pool degrades to an unclear attempt; unclear defers
  at most MAX_SCORE_ATTEMPTS times, then finalizes);
- scores roll up to the source as a derived calibration view, and
  retrieval says the standing plainly wherever the source is rendered.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import predictions as pred_mod
from lisan.tools.predictions import (
    MAX_SCORE_ATTEMPTS,
    calibration_standing,
    has_due_predictions,
    record_prediction,
    rollup_calibration,
    run_prediction_reconcile,
)
from lisan.tools.record_factory import new_pattern
from lisan.tools.validator import validate_vault


def _future(days: int = 14) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class PredictionLedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"
        from lisan.tools.record_factory import new_evidence

        seed = new_evidence(
            self.vault,
            title="Check-in — Wren — after the reunion",
            source_type="checkin",
            actors=["Wren"],
            observed_facts=["quiet all evening after the reunion"],
            summary="Wren quiet after the reunion",
        )
        seed_id = str(load_markdown(seed.path).frontmatter["id"])
        created = new_pattern(
            self.vault,
            pattern_type="relational_loop",
            hypothesis="Wren goes quiet for a day after large family gatherings",
            alternative_explanations=["Ordinary tiredness after busy days"],
            supporting_records=[seed_id],
            evidence_needed=["More dated observations across different gatherings"],
        )
        self.source_path = created.path
        self.source_id = str(load_markdown(created.path).frontmatter["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, expectation: str = "Wren will be quiet the day after the picnic", **kw):
        defaults = dict(source=self.source_id, review_after=_future(), db_path=self.db)
        defaults.update(kw)
        return record_prediction(self.vault, expectation, **defaults)

    def _make_due(self, path: Path) -> None:
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm["review_after"] = (date.today() - timedelta(days=1)).isoformat()
        write_markdown(path, fm, doc.body)


class CreationGateTests(PredictionLedgerBase):
    def test_happy_path_creates_valid_pending_record(self):
        out = self._record()
        self.assertTrue(out["ok"], out)
        fm = load_markdown(Path(out["path"])).frontmatter
        self.assertEqual(fm["status"], "pending")
        self.assertEqual(fm["source_id"], self.source_id)
        self.assertIn(self.source_id, fm["links"])
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())

    def test_unknown_source_is_refused(self):
        out = self._record(source="pattern.does-not-exist")
        self.assertFalse(out["ok"])
        self.assertIn("does not resolve", out["error"])

    def test_clinical_language_is_refused(self):
        out = self._record(expectation="Wren's narcissistic behavior will recur at the picnic")
        self.assertFalse(out["ok"])
        self.assertIn("language gate", out["error"])

    def test_past_review_date_is_refused(self):
        out = self._record(review_after="2020-01-01")
        self.assertFalse(out["ok"])
        self.assertIn("future", out["error"])

    def test_due_detection(self):
        out = self._record()
        self.assertFalse(has_due_predictions(self.vault))
        self._make_due(Path(out["path"]))
        self.assertTrue(has_due_predictions(self.vault))


class ReconcileScoringTests(PredictionLedgerBase):
    def _due_prediction(self) -> Path:
        out = self._record()
        path = Path(out["path"])
        self._make_due(path)
        return path

    def _pool(self):
        return [
            {"id": "evidence.picnic-checkin", "date": date.today().isoformat(),
             "type": "evidence", "summary": "Check-in: quiet evening after the picnic"},
            {"id": "episode.picnic-day", "date": date.today().isoformat(),
             "type": "episode", "summary": "The family picnic happened"},
        ]

    def test_hit_is_applied_and_idempotent(self):
        path = self._due_prediction()
        with patch.object(pred_mod, "_evidence_pool", return_value=self._pool()), \
                patch.object(pred_mod, "_judge", return_value={
                    "verdict": "hit", "evidence_refs": ["evidence.picnic-checkin"], "reason": "the check-in shows it",
                }):
            summary = run_prediction_reconcile(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["hits"], 1)
        fm = load_markdown(path).frontmatter
        self.assertEqual(fm["status"], "scored")
        self.assertEqual(fm["verdict"], "hit")
        self.assertEqual(fm["verdict_evidence"], ["evidence.picnic-checkin"])
        before = path.read_text(encoding="utf-8")
        # Second run: nothing due, nothing touched, judge never called.
        with patch.object(pred_mod, "_judge", side_effect=AssertionError("re-scored a scored prediction")):
            summary2 = run_prediction_reconcile(vault=self.vault, db_path=self.db)
        self.assertEqual(summary2["due"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_fabricated_evidence_degrades_to_unclear_attempt(self):
        path = self._due_prediction()
        with patch.object(pred_mod, "_evidence_pool", return_value=self._pool()), \
                patch.object(pred_mod, "_judge", return_value={
                    "verdict": "miss", "evidence_refs": ["claim.invented-record"], "reason": "trust me",
                }):
            summary = run_prediction_reconcile(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["misses"], 0)
        self.assertEqual(summary["deferred"], 1)
        fm = load_markdown(path).frontmatter
        self.assertEqual(fm["status"], "pending")
        self.assertEqual(fm["score_attempts"], 1)
        self.assertIn("discarded", fm["verdict_note"])
        self.assertGreater(str(fm["review_after"]), date.today().isoformat())

    def test_empty_pool_defers_without_calling_the_judge(self):
        path = self._due_prediction()
        with patch.object(pred_mod, "_evidence_pool", return_value=[]), \
                patch.object(pred_mod, "_judge", side_effect=AssertionError("judged an empty pool")):
            summary = run_prediction_reconcile(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(load_markdown(path).frontmatter["score_attempts"], 1)

    def test_unclear_finalizes_after_bounded_attempts(self):
        path = self._due_prediction()
        for attempt in range(MAX_SCORE_ATTEMPTS):
            self._make_due(path)
            with patch.object(pred_mod, "_evidence_pool", return_value=self._pool()), \
                    patch.object(pred_mod, "_judge", return_value={
                        "verdict": "unclear", "evidence_refs": [], "reason": "pool does not settle it",
                    }):
                run_prediction_reconcile(vault=self.vault, db_path=self.db)
        fm = load_markdown(path).frontmatter
        self.assertEqual(fm["status"], "scored")
        self.assertEqual(fm["verdict"], "unclear")
        self.assertEqual(fm["score_attempts"], MAX_SCORE_ATTEMPTS)
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())


class CalibrationRollupTests(PredictionLedgerBase):
    def test_standing_words(self):
        self.assertEqual(calibration_standing({"hits": 0, "misses": 0, "unclear": 0, "pending": 2}), "unproven")
        self.assertEqual(calibration_standing({"hits": 1, "misses": 0, "unclear": 0, "pending": 0}), "early")
        self.assertEqual(calibration_standing({"hits": 4, "misses": 1, "unclear": 0, "pending": 0}), "predicting well")
        self.assertEqual(calibration_standing({"hits": 1, "misses": 4, "unclear": 1, "pending": 0}), "keeps being surprised")
        self.assertEqual(calibration_standing({"hits": 2, "misses": 2, "unclear": 0, "pending": 1}), "mixed")

    def test_scores_roll_up_to_the_source(self):
        out = self._record()
        path = Path(out["path"])
        self._make_due(path)
        with patch.object(pred_mod, "_evidence_pool", return_value=[
            {"id": "evidence.x", "date": date.today().isoformat(), "type": "evidence", "summary": "it happened"},
        ]), patch.object(pred_mod, "_judge", return_value={
            "verdict": "hit", "evidence_refs": ["evidence.x"], "reason": "shown",
        }):
            summary = run_prediction_reconcile(vault=self.vault, db_path=self.db)
        self.assertEqual(summary["sources_updated"], 1)
        cal = load_markdown(self.source_path).frontmatter["prediction_calibration"]
        self.assertEqual(cal["hits"], 1)
        self.assertEqual(cal["standing"], "early")
        # Rollup is a derived view: recomputing changes nothing.
        rollup_calibration(self.vault, self.source_id, db_path=self.db)
        cal2 = load_markdown(self.source_path).frontmatter["prediction_calibration"]
        self.assertEqual({k: cal2[k] for k in ("hits", "misses", "unclear", "pending", "standing")},
                         {k: cal[k] for k in ("hits", "misses", "unclear", "pending", "standing")})


class RenderingTests(PredictionLedgerBase):
    def test_calibration_line_renders_with_the_pattern(self):
        from lisan.tools.retrieval import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem

        doc = load_markdown(self.source_path)
        fm = dict(doc.frontmatter)
        fm["prediction_calibration"] = {"hits": 4, "misses": 1, "unclear": 0, "pending": 2, "standing": "predicting well", "updated": "2026-07-15"}
        write_markdown(self.source_path, fm, doc.body)
        item = RetrievalItem(
            id=self.source_id, type="pattern",
            path=str(self.source_path.relative_to(self.vault)),
            summary="", score=1.0, reason="test",
        )
        detail = _format_item_detail(item, self.source_path, lean=True)
        self.assertIn("prediction record: 4 hit / 1 miss", detail)
        self.assertIn("predicting well", detail)

    def test_prediction_record_renders_with_verdict(self):
        from lisan.tools.retrieval import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem

        out = self._record()
        path = Path(out["path"])
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm.update({"status": "scored", "verdict": "miss", "scored_at": "2026-07-20",
                   "verdict_evidence": ["evidence.y"], "verdict_note": "did not happen"})
        write_markdown(path, fm, doc.body)
        item = RetrievalItem(
            id=str(fm["id"]), type="prediction",
            path=str(path.relative_to(self.vault)), summary="", score=1.0, reason="test",
        )
        detail = _format_item_detail(item, path, lean=True)
        self.assertIn("verdict: miss", detail)
        self.assertIn("did not happen", detail)


class JobWiringTests(PredictionLedgerBase):
    def test_dispatch_routes_to_reconcile(self):
        from lisan.tools.jobs import dispatch_job

        with patch.object(pred_mod, "run_prediction_reconcile", return_value={"due": 0}) as run:
            out = dispatch_job(
                {"id": "job.x", "job_type": "prediction.reconcile", "payload": {"vault": str(self.vault)}},
                vault=self.vault, db_path=self.db,
            )
        self.assertTrue(run.called)
        self.assertEqual(out, {"due": 0})

    def test_post_turn_planner_queues_only_when_due(self):
        from lisan.tools.job_policy import which_jobs_for_turn

        metadata = {
            "vault": str(self.vault),
            "conversation_id": "c1",
            "text": "a substantial turn about the family picnic plans",
            "action": "full",
            "mode": "structured",
        }
        jobs = which_jobs_for_turn(metadata, db_path=self.db)
        self.assertNotIn("prediction.reconcile", {j["job_type"] for j in jobs})
        out = self._record()
        self._make_due(Path(out["path"]))
        jobs = which_jobs_for_turn(metadata, db_path=self.db)
        self.assertIn("prediction.reconcile", {j["job_type"] for j in jobs})


if __name__ == "__main__":
    unittest.main()
