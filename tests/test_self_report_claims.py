"""WO-GROUND Seam B: self-reports can never impersonate observations.

The 2026-07-06 poison record — an agent-authored claim about its own
internals stored at confidence 1.0 with basis "direct observation of its
internal logs" — must be structurally impossible. The claim factory is the
single seam every creator flows through (writer fanout, CLI, ingestion),
and the validator refuses the shape wherever it appears.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.record_factory import (
    SELF_REPORT_CONFIDENCE_CAP,
    new_claim,
    normalize_claim_class,
)
from lisan.tools.validator import validate_vault


class SelfReportFactoryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_poison_record_shape_is_impossible(self):
        # The exact 2026-07-06 shape: agent-owned, operational subject,
        # confidence 1.0, basis asserting observation, no linked evidence.
        created = new_claim(
            self.vault,
            "The reminder failure was caused by a stalled task processor due to a database issue.",
            owner="agent",
            claim_class="observation",
            confidence=1.0,
            confidence_basis="direct observation of its internal logs",
        )
        fm = load_markdown(created.path).frontmatter
        self.assertEqual(fm["claim_class"], "self_report")
        self.assertLessEqual(float(fm["confidence"]), SELF_REPORT_CONFIDENCE_CAP)
        self.assertEqual(fm["confidence_basis"], "agent self-report, unverified")

    def test_linked_evidence_keeps_the_stated_basis(self):
        # With a linked tool result the basis MAY assert observation; the
        # confidence cap still holds — it's a self-report either way.
        created = new_claim(
            self.vault,
            "The telegram service restarted successfully at 15:26.",
            owner="agent",
            claim_class="observation",
            confidence=0.95,
            supporting_evidence=["evidence.restart-tool-output"],
            confidence_basis="tool output: launchctl kickstart returned success",
        )
        fm = load_markdown(created.path).frontmatter
        self.assertEqual(fm["claim_class"], "self_report")
        self.assertLessEqual(float(fm["confidence"]), SELF_REPORT_CONFIDENCE_CAP)
        self.assertIn("tool output", fm["confidence_basis"])

    def test_agent_claims_about_the_world_are_untouched(self):
        # Agent-owned but not operational: not a self-report.
        created = new_claim(
            self.vault,
            "Allowing pork to come to room temperature before cooking results in it cooking more evenly.",
            owner="agent",
            claim_class="inference",
            confidence=0.8,
        )
        fm = load_markdown(created.path).frontmatter
        self.assertEqual(fm["claim_class"], "inference")
        self.assertEqual(float(fm["confidence"]), 0.8)

    def test_user_claims_are_untouched(self):
        created = new_claim(
            self.vault,
            "The reminder system matters to me — I rely on it every morning.",
            owner="user",
            claim_class="value_statement",
            confidence=0.9,
        )
        fm = load_markdown(created.path).frontmatter
        self.assertEqual(fm["claim_class"], "value_statement")
        self.assertEqual(float(fm["confidence"]), 0.9)

    def test_self_report_is_a_legal_class(self):
        self.assertEqual(normalize_claim_class("self_report"), "self_report")


class SelfReportValidatorGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hand_written_overconfident_self_report_fails_validation(self):
        # A record that bypassed the factory (hand edit, old vault, rogue
        # writer) must still be refused by the validator.
        created = new_claim(
            self.vault,
            "The scheduler is running normally.",
            owner="agent",
            confidence=0.5,
        )
        doc = load_markdown(created.path)
        fm = dict(doc.frontmatter)
        fm["confidence"] = 1.0
        from lisan.frontmatter import write_markdown

        write_markdown(created.path, fm, doc.body)
        report = validate_vault(self.vault)
        messages = " | ".join(issue.message for issue in report.issues)
        self.assertIn("capped at medium", messages)


if __name__ == "__main__":
    unittest.main()
