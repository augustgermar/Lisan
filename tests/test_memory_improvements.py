from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisan.tools.context_budget import bound_final_context
from lisan.tools.entity_story import shared_index_text
from lisan.tools.freshness import metadata
from lisan.tools.judge import validate_score_ids


class MemoryImprovementTests(unittest.TestCase):
    def test_final_context_budget_is_hard_and_reports_omission(self) -> None:
        bounded, accounting = bound_final_context("a" * 500, budget_chars=120)
        self.assertLessEqual(len(bounded), 120)
        self.assertEqual(accounting["retrieved_chars"], 500)
        self.assertTrue(accounting["omitted"])

    def test_shared_index_text_keeps_source_log_after_compaction(self) -> None:
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "entity.log"
            archive.write_text('{"text": "Ruth documented cobalt migration.", "date": "2025-01-02"}\n')
            text = shared_index_text(
                {"canonical_name": "Ruth", "source_log": [
                    {"text": "Ruth prefers the cedar room.", "date": "2025-01-03"}
                ]},
                "# Ruth\n\nRuth lives near the coast.",
                archive_path=archive,
            )
        self.assertIn("cobalt migration", text)
        self.assertIn("cedar room", text)
        self.assertIn("lives near the coast", text)

    def test_missing_observation_date_is_not_now_and_review_due_is_not_expiry(self) -> None:
        fresh = metadata({"summary": "undated fact", "review_after": "2000-01-01"})
        self.assertEqual(fresh.observed_at, "unknown")
        self.assertEqual(fresh.status, "unknown")
        dated = metadata({"observed_at": "2020-01-01", "review_after": "2000-01-01"})
        self.assertEqual(dated.status, "current")
        self.assertIsNone(dated.valid_until)

    def test_judge_scores_require_exact_rubric_ids(self) -> None:
        rubric = {"dimensions": [{"id": "continuity"}, {"id": "grounding"}]}
        self.assertEqual(
            validate_score_ids([{"id": "continuity", "score": 5}], rubric)[0],
            False,
        )
        self.assertEqual(
            validate_score_ids(
                [{"id": "continuity", "score": 5}, {"id": "grounding", "score": None}],
                rubric,
            )[0],
            True,
        )


if __name__ == "__main__":
    unittest.main()
