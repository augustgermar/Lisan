from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.seed_evaluation import run_seed_evaluation
from lisan.tools.validator import validate_vault


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "self_model_seed"
FIXTURE_VAULT = FIXTURE_ROOT / "vault"
FIXTURE_QUARANTINE = FIXTURE_ROOT / "quarantine" / "diagnostic_pattern.md"


class SeedEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        shutil.copytree(FIXTURE_VAULT, self.vault, dirs_exist_ok=True)
        self.db_path = self.root / "lisan.sqlite"
        self.embeddings_path = self.root / "embeddings.bin"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seed_vault_validates_before_evaluation(self) -> None:
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())

    def test_seed_evaluation_generates_patterns_and_blocks_dreamer(self) -> None:
        result = run_seed_evaluation(
            vault=self.vault,
            db_path=self.db_path,
            embeddings_file=self.embeddings_path,
        )
        self.assertTrue(result.validation_after.ok, result.validation_after.summary())
        self.assertTrue(result.patterns)
        authority_patterns = [row for row in result.patterns if row.pattern_type == "authority_response"]
        self.assertTrue(authority_patterns)
        self.assertTrue(any(len(row.supporting_records) >= 2 for row in authority_patterns))
        self.assertTrue(any(row.counterexamples for row in result.patterns))
        self.assertTrue(any(not row.dreamer_eligible for row in result.patterns))
        self.assertTrue(any(row.blocked_reasons for row in result.patterns))
        self.assertIn("Dreamer Audit", result.report_text)
        self.assertIn("blocked_reason", result.report_text)

    def test_hostile_motive_claim_stays_disputed(self) -> None:
        result = run_seed_evaluation(
            vault=self.vault,
            db_path=self.db_path,
            embeddings_file=self.embeddings_path,
        )
        claim = load_markdown(self.vault / "claims" / "2026-05-01-scapegoat-risk.md").frontmatter
        review = load_markdown(self.vault / "reviews" / "2026-05-01-scapegoat-risk-review.md").frontmatter
        self.assertEqual(claim.get("status"), "disputed")
        self.assertEqual(review.get("approved"), False)
        self.assertIn("mind_reading", review.get("reasoning_errors", []))
        self.assertLess(float(claim.get("confidence") or 0.0), 0.5)
        self.assertIn("counterexamples", result.report_text)

    def test_overgeneralized_identity_claim_is_downgraded(self) -> None:
        run_seed_evaluation(
            vault=self.vault,
            db_path=self.db_path,
            embeddings_file=self.embeddings_path,
        )
        claim = load_markdown(self.vault / "claims" / "2026-05-02-avoid-visibility.md").frontmatter
        review = load_markdown(self.vault / "reviews" / "2026-05-02-avoid-visibility-review.md").frontmatter
        self.assertEqual(claim.get("status"), "disputed")
        self.assertIn("overgeneralization", review.get("reasoning_errors", []))
        self.assertIn("all_or_nothing_thinking", review.get("reasoning_errors", []))
        self.assertLess(float(claim.get("confidence") or 0.0), 0.5)

    def test_diagnostic_language_trap_is_rejected(self) -> None:
        shutil.copy2(FIXTURE_QUARANTINE, self.vault / "patterns" / "diagnostic_pattern.md")
        report = validate_vault(self.vault)
        self.assertFalse(report.ok)
        self.assertTrue(any("diagnostic or pathologizing language" in issue.message for issue in report.issues))

    def test_seed_report_is_readable(self) -> None:
        result = run_seed_evaluation(
            vault=self.vault,
            db_path=self.db_path,
            embeddings_file=self.embeddings_path,
        )
        self.assertIn("## Pattern Summary", result.report_text)
        self.assertIn("## Dreamer Audit", result.report_text)
        self.assertIn("skeptic_approved", result.report_text)
        self.assertIn("approved_for_dreamer", result.report_text)


if __name__ == "__main__":
    unittest.main()
