from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.analyst_ops import build_analyst_bundle, run_analyst_scan
from lisan.tools.epistemic import review_claim_against_evidence
from lisan.tools.record_factory import new_claim, new_evidence, new_pattern, new_skeptical_review
from lisan.tools.retrieval import assemble_context
from lisan.tools.dreamer_ops import _bundle_approved_patterns, audit_patterns
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.validator import validate_vault


class EvidenceClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        self.embeddings_path = self.root / "embeddings.bin"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _frontmatter(self, path: Path) -> dict[str, object]:
        return load_markdown(path).frontmatter

    def _backdate_record(self, path: Path, days: int = 10) -> None:
        doc = load_markdown(path)
        backdate = (date.today() - timedelta(days=days)).isoformat()
        updated = dict(doc.frontmatter)
        for field in ["created", "created_at", "updated", "first_seen", "last_reviewed", "review_after", "last_confirmed"]:
            if field in updated and updated[field] is not None:
                updated[field] = backdate
        write_markdown(path, updated, doc.body)

    def test_neutral_email_limits_hostile_interpretation(self) -> None:
        evidence = new_evidence(
            vault=self.vault,
            title="Rollout planning email",
            source_type="email",
            source_uri="mail://thread/123",
            actors=["Person A", "Person B"],
            arena="work",
            compartments=[],
            reliability="high",
            summary="Person A asked Person B to present the project rollout plan to management.",
            observed_facts=[
                "Person A asked Person B to present the rollout plan.",
                "The message references management and a project rollout plan.",
            ],
            linked_episodes=[],
        )
        review = review_claim_against_evidence(
            "Person A tried to scapegoat Person B.",
            evidence_items=[self._frontmatter(evidence.path)],
        )
        self.assertEqual(review["status"], "disputed")
        self.assertIn("mind_reading", review["reasoning_errors"])
        self.assertLess(review["confidence"], 0.5)
        self.assertTrue(review["alternative_hypotheses"])

    def test_calendar_or_ticket_does_not_overstate_intent(self) -> None:
        evidence = new_evidence(
            vault=self.vault,
            title="Planning ticket",
            source_type="ticket",
            source_uri="ticket://JIRA-42",
            actors=["Person B", "Team"],
            arena="work",
            reliability="medium",
            summary="A planning ticket assigned Person B to prepare a rollout brief.",
            observed_facts=["A ticket assigned Person B to prepare a rollout brief."],
        )
        review = review_claim_against_evidence(
            "This proves leadership trusts Person B with strategy.",
            evidence_items=[self._frontmatter(evidence.path)],
        )
        self.assertIn(review["recommended_action"], {"revise", "hold"})
        self.assertLess(review["confidence"], 0.75)

    def test_journal_only_claim_stays_tentative(self) -> None:
        review = review_claim_against_evidence("I feel like everyone is upset with me.")
        self.assertGreaterEqual(len(review["evidence_needed"]), 1)
        self.assertLess(review["confidence"], 0.5)
        self.assertIn("insufficient_alternative_hypotheses", review["reasoning_errors"])

    def test_multiple_evidence_supports_claim(self) -> None:
        evidence_a = new_evidence(
            vault=self.vault,
            title="Rollout request",
            source_type="email",
            source_uri="mail://thread/alpha",
            actors=["Manager", "Person B"],
            arena="work",
            reliability="high",
            summary="Manager asked Person B to present the rollout plan.",
            observed_facts=["Manager asked Person B to present the rollout plan."],
        )
        evidence_b = new_evidence(
            vault=self.vault,
            title="Follow-up note",
            source_type="document",
            source_uri="doc://notes/77",
            actors=["Manager", "Person B"],
            arena="work",
            reliability="high",
            summary="The follow-up note confirmed Person B would present the rollout plan.",
            observed_facts=["The note confirmed Person B would present the rollout plan."],
        )
        review = review_claim_against_evidence(
            "Manager asked Person B to present the rollout plan.",
            evidence_items=[self._frontmatter(evidence_a.path), self._frontmatter(evidence_b.path)],
        )
        self.assertTrue(review["supporting_evidence"])
        self.assertGreater(review["confidence"], 0.5)
        self.assertIn(review["status"], {"active", "confirmed"})

    def test_contradictory_evidence_reduces_confidence(self) -> None:
        evidence = new_evidence(
            vault=self.vault,
            title="Clarifying email",
            source_type="email",
            source_uri="mail://thread/beta",
            actors=["Person A", "Person B"],
            arena="work",
            reliability="high",
            summary="There is no evidence of blame or secrecy in the email.",
            observed_facts=["The email did not mention blame.", "The email did not mention secrecy."],
        )
        review = review_claim_against_evidence(
            "Person A was trying to hide the rollout.",
            evidence_items=[self._frontmatter(evidence.path)],
        )
        self.assertTrue(review["contradicting_evidence"])
        self.assertIn(review["recommended_action"], {"revise", "hold"})
        self.assertLess(review["confidence"], 0.5)

    def test_index_validation_and_retrieval_prioritize_evidence(self) -> None:
        evidence = new_evidence(
            vault=self.vault,
            title="Rollout email",
            source_type="email",
            source_uri="mail://thread/gamma",
            actors=["Person A", "Person B"],
            arena="work",
            reliability="high",
            summary="Person A asked Person B to present the rollout plan to management.",
            observed_facts=["Person A asked Person B to present the rollout plan to management."],
            linked_episodes=[],
        )
        claim = new_claim(
            vault=self.vault,
            claim_text="Person A was trying to scapegoat Person B.",
            claim_class="motive_hypothesis",
            owner="user",
            status="disputed",
            confidence=0.2,
            supporting_evidence=[],
            contradicting_evidence=[str(self._frontmatter(evidence.path)["id"])],
            linked_patterns=["scapegoat"],
            arena="work",
            summary="A hostile interpretation of the rollout request.",
        )
        claim_id = str(self._frontmatter(claim.path)["id"])
        review = new_skeptical_review(
            vault=self.vault,
            reviewed_record_id=claim_id,
            reviewed_record_type="claim",
            summary="Skeptic review of the scapegoat interpretation.",
            approved=False,
            risk="medium",
            recommended_action="revise",
            issues=[{"type": "mind_reading", "message": "The claim overreads intent."}],
            priority_questions=["What did the email actually say?"],
            alternative_hypotheses=["Delegation", "normal coordination"],
            evidence_needed=["Direct wording from the email"],
            claim_updates=[{"claim_text": "Person A was trying to scapegoat Person B.", "status": "disputed"}],
            confidence_adjustments=[{"target": str(self._frontmatter(evidence.path)["id"]), "delta": 0.1}],
            reasoning_errors=["mind_reading"],
        )
        self.assertTrue(review.path.exists())

        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())

        rebuild_index(vault=self.vault, db_path=self.db_path, embeddings_file=self.embeddings_path)
        context = assemble_context("project rollout management", vault=self.vault, db_path=self.db_path)
        assembled = context
        self.assertIn("## Evidence", assembled)
        self.assertIn("## Claims", assembled)
        self.assertLess(assembled.index("## Evidence"), assembled.index("## Claims"))

    def test_analyst_creates_and_reviews_patterns(self) -> None:
        new_evidence(
            vault=self.vault,
            title="Avoidance note one",
            source_type="journal",
            arena="work",
            summary="I kept avoiding the manager follow-up even though I knew it was due.",
            observed_facts=["The note says the user kept avoiding the manager follow-up."],
            reliability="medium",
        )
        new_evidence(
            vault=self.vault,
            title="Avoidance note two",
            source_type="journal",
            arena="work",
            summary="I delayed the work reply again and avoided the decision.",
            observed_facts=["The note says the user delayed the work reply and avoided the decision."],
            reliability="medium",
        )
        evidence_three = new_evidence(
            vault=self.vault,
            title="Avoidance note three",
            source_type="journal",
            arena="work",
            summary="I put off the manager update one more time.",
            observed_facts=["The note says the user put off the manager update."],
            reliability="medium",
        )
        result = run_analyst_scan(vault=self.vault)
        self.assertTrue(result.pattern_paths)
        self.assertTrue(result.review_paths)
        for pattern_path in result.pattern_paths:
            self._backdate_record(pattern_path, days=10)
            pattern = load_markdown(pattern_path).frontmatter
            self.assertEqual(pattern.get("type"), "pattern")
            self.assertGreaterEqual(len(pattern.get("supporting_records") or []), 2)
            self.assertIn("counterexample_search", pattern)
            self.assertTrue(pattern.get("counterexample_search", {}).get("performed"))
        bundle = _bundle_approved_patterns(self.vault)
        self.assertIn("Approved Pattern Hypotheses", bundle)
        self.assertNotIn("- None", bundle)
        audit = audit_patterns(self.vault)
        self.assertTrue(audit["eligible"])

    def test_pattern_without_counterexample_search_is_blocked_from_dreamer(self) -> None:
        pattern = new_pattern(
            vault=self.vault,
            pattern_type="work_loop",
            hypothesis="Work visibility keeps recurring in the same way.",
            supporting_records=["e1", "e2", "e3"],
            counterexamples=["No explicit counterexamples found in the scanned records."],
            alternative_explanations=["Visibility may be increasing because of routine team coordination."],
            confidence=0.82,
            status="skeptic_reviewed",
            counterexample_search={"performed": False, "search_terms": ["visibility"], "result_summary": "Not run", "counterexamples": []},
            strength_override=False,
            integration_override={"enabled": False, "reason": "", "approved_by": ""},
        )
        self._backdate_record(pattern.path, days=10)
        review = new_skeptical_review(
            vault=self.vault,
            reviewed_record_id=self._frontmatter(pattern.path)["id"],
            reviewed_record_type="pattern",
            summary="Pattern review ready for Dreamer.",
            approved=True,
            approved_for_dreamer=True,
            risk="low",
            recommended_action="approve",
            issues=[],
            priority_questions=[],
            alternative_hypotheses=["Routine coordination"],
            evidence_needed=["Later evidence should confirm the boundary."],
            reasoning_errors=[],
            pattern_status="skeptic_reviewed",
        )
        self.assertTrue(review.path.exists())
        bundle = _bundle_approved_patterns(self.vault)
        self.assertIn("- None", bundle)
        audit = audit_patterns(self.vault)
        reasons = " ".join(" ".join(entry["reasons"]) for entry in audit["blocked"])
        self.assertIn("counterexample_search.performed=false", reasons)

    def test_overgeneralized_visibility_narrative_is_not_promoted(self) -> None:
        new_evidence(
            vault=self.vault,
            title="Visibility note one",
            source_type="journal",
            arena="work",
            summary="I always avoid visibility when a meeting is uncertain.",
            observed_facts=["The note says the user avoided visibility in one meeting."],
            reliability="medium",
        )
        new_evidence(
            vault=self.vault,
            title="Visibility note two",
            source_type="journal",
            arena="work",
            summary="I still shared the update even though I was uneasy.",
            observed_facts=["The note says the user shared an update."],
            reliability="medium",
        )
        result = run_analyst_scan(vault=self.vault)
        self.assertTrue(result.pattern_paths)
        audit = audit_patterns(self.vault)
        blocked_text = " ".join(" ".join(entry["reasons"]) for entry in audit["blocked"])
        self.assertIn("supporting_records<3", blocked_text)
        self.assertIn("age<7d", blocked_text)

    def test_diagnostic_language_fails_validation(self) -> None:
        pattern = new_pattern(
            vault=self.vault,
            pattern_type="other",
            hypothesis="This is a narcissistic pattern.",
            supporting_records=["a", "b", "c"],
            counterexamples=["No explicit counterexamples found in the scanned records."],
            alternative_explanations=["The wording may be imprecise."],
            confidence=0.8,
            status="candidate",
            counterexample_search={"performed": True, "search_terms": ["narcissistic"], "result_summary": "Counterexample search performed.", "counterexamples": ["No explicit counterexamples found in the scanned records."]},
            strength_override=False,
            integration_override={"enabled": False, "reason": "", "approved_by": ""},
        )
        report = validate_vault(self.vault)
        self.assertFalse(report.ok)
        self.assertTrue(any("diagnostic or pathologizing language" in issue.message for issue in report.issues))


if __name__ == "__main__":
    unittest.main()
