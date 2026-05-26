from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.record_factory import new_claim, new_evidence, new_pattern
from lisan.tools.retrieval import assemble_context, retrieve_context
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.validator import validate_vault
from lisan.utils import slugify


class GraphRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        self.embeddings_path = self.root / "embeddings.bin"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rebuild(self) -> None:
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())
        rebuild_index(vault=self.vault, db_path=self.db_path, embeddings_file=self.embeddings_path)

    def _record_id(self, record_type: str, text: str) -> str:
        if record_type == "evidence":
            return f"evidence.{slugify(text)}"
        if record_type == "claim":
            return f"claim.{slugify(text)[:80]}"
        if record_type == "pattern":
            return f"pattern.{slugify(text)[:80]}"
        raise ValueError(record_type)

    def _retarget_record(self, path: Path, *, domain_primary: str, allowed_contexts: list[str] | None = None, blocked_contexts: list[str] | None = None, compartments: list[str] | None = None) -> None:
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm["domain_primary"] = domain_primary
        fm["arena"] = domain_primary
        fm["allowed_contexts"] = allowed_contexts if allowed_contexts is not None else ["all"]
        fm["blocked_contexts"] = blocked_contexts if blocked_contexts is not None else []
        fm["compartments"] = compartments if compartments is not None else []
        write_markdown(path, fm, doc.body)

    def test_direct_evidence_retrieves_linked_claim(self) -> None:
        claim_text = "Person A may be setting me up as a scapegoat."
        claim_id = self._record_id("claim", claim_text)
        evidence_title = "Rollout request"
        evidence_id = self._record_id("evidence", evidence_title)
        claim = new_claim(
            vault=self.vault,
            claim_text=claim_text,
            claim_class="motive_hypothesis",
            owner="user",
            status="disputed",
            confidence=0.25,
            supporting_evidence=[],
            contradicting_evidence=[],
            linked_patterns=[],
            arena="status",
            summary="A hostile interpretation of the message.",
        )
        self._retarget_record(claim.path, domain_primary="status")

        new_evidence(
            vault=self.vault,
            title=evidence_title,
            source_type="email",
            source_uri="mail://thread/123",
            actors=["Person A", "Person B"],
            arena="work",
            reliability="high",
            summary="Person A asked Person B to present the project rollout plan to management.",
            observed_facts=["Person A asked Person B to present the project rollout plan to management."],
            linked_claims=[claim_id],
            linked_episodes=[],
        )
        self._rebuild()

        result = retrieve_context("work relationship project rollout plan", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == evidence_id for item in result.direct_loaded))
        self.assertTrue(any(item.id == claim_id for item in result.expanded_loaded))
        claim_item = next(item for item in result.expanded_loaded if item.id == claim_id)
        self.assertEqual(claim_item.expansion_source, evidence_id)
        self.assertIn("linked_claims", claim_item.expansion_reason)

        context = assemble_context("work relationship project rollout plan", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertIn("expansion_source", context)
        self.assertIn("expansion_reason", context)

    def test_claim_retrieves_supporting_evidence(self) -> None:
        claim_text = "Person B should approve the proposal."
        claim_id = self._record_id("claim", claim_text)
        evidence_title = "Coordination file"
        evidence_id = self._record_id("evidence", evidence_title)
        evidence = new_evidence(
            vault=self.vault,
            title=evidence_title,
            source_type="document",
            source_uri="doc://notes/42",
            actors=["Team"],
            arena="status",
            reliability="high",
            summary="A coordination note confirmed a follow-up assignment.",
            observed_facts=["The note confirmed a follow-up assignment."],
            linked_claims=[],
            linked_episodes=[],
        )
        self._retarget_record(evidence.path, domain_primary="status")

        new_claim(
            vault=self.vault,
            claim_text=claim_text,
            claim_class="observation",
            owner="user",
            status="active",
            confidence=0.7,
            supporting_evidence=[evidence_id],
            contradicting_evidence=[],
            linked_patterns=[],
            arena="work",
            summary="Z direct claim about who should approve the zebra.",
        )
        for idx in range(16):
            new_claim(
                vault=self.vault,
                claim_text=f"Person B should approve the proposal {idx}",
                claim_class="observation",
                owner="user",
                status="active",
                confidence=0.2,
                supporting_evidence=[],
                contradicting_evidence=[],
                linked_patterns=[],
                arena="work",
                summary=f"A noise record {idx}.",
            )
        self._rebuild()

        result = retrieve_context("work relationship proposal approval", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == claim_id for item in result.direct_loaded))
        self.assertTrue(any(item.id == evidence_id for item in result.expanded_loaded))
        evidence_item = next(item for item in result.expanded_loaded if item.id == evidence_id)
        self.assertEqual(evidence_item.expansion_source, claim_id)
        self.assertIn("supporting_evidence", evidence_item.expansion_reason)

    def test_pattern_retrieves_counterexample(self) -> None:
        support_a = self._record_id("evidence", "Manager follow-up one")
        support_b = self._record_id("evidence", "Manager follow-up two")
        counterexample = self._record_id("evidence", "Positive demo")
        pattern_hypothesis = "I avoid visibility when a work request feels uncertain."
        pattern_id = f"pattern.{slugify(f'avoidance_loop-{pattern_hypothesis}')[:80]}"
        evidence_a = new_evidence(
            vault=self.vault,
            title="Manager follow-up one",
            source_type="journal",
            arena="work",
            summary="I kept avoiding the manager follow-up even though it was due.",
            observed_facts=["The note says the user kept avoiding the manager follow-up."],
            reliability="medium",
            linked_claims=[],
            linked_episodes=[],
        )
        evidence_b = new_evidence(
            vault=self.vault,
            title="Manager follow-up two",
            source_type="journal",
            arena="work",
            summary="I delayed the work reply again and avoided the decision.",
            observed_facts=["The note says the user delayed the work reply and avoided the decision."],
            reliability="medium",
            linked_claims=[],
            linked_episodes=[],
        )
        counter = new_evidence(
            vault=self.vault,
            title="Positive demo",
            source_type="journal",
            arena="status",
            summary="I volunteered to present the update at the all-hands.",
            observed_facts=["The note says the user volunteered to present the update."],
            reliability="medium",
            linked_claims=[],
            linked_episodes=[],
        )
        self._retarget_record(counter.path, domain_primary="status")
        new_pattern(
            vault=self.vault,
            pattern_type="avoidance_loop",
            hypothesis=pattern_hypothesis,
            supporting_records=[support_a, support_b],
            counterexamples=[counterexample],
            alternative_explanations=["The pattern may reflect temporary workload pressure."],
            confidence=0.68,
            status="skeptic_reviewed",
            first_seen="2026-05-01",
            last_reviewed="2026-05-01",
            predictions=["The user may continue avoiding visibility when stressed."],
            review_notes="Seeded for graph traversal tests.",
            evidence_needed=["A future example that breaks the loop."],
            counterexample_search={"performed": True, "search_terms": ["visibility"], "result_summary": "Counterexample search completed.", "counterexamples": [counterexample]},
            arena="work",
        )
        self._retarget_record(counter.path, domain_primary="status")
        self._rebuild()

        result = retrieve_context("work relationship visibility loop", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == pattern_id for item in result.direct_loaded))
        self.assertTrue(any(item.id == counterexample for item in result.expanded_loaded))
        counterexample_item = next(item for item in result.expanded_loaded if item.id == counterexample)
        self.assertEqual(counterexample_item.expansion_source, pattern_id)
        self.assertIn("counterexamples", counterexample_item.expansion_reason)

    def test_compartment_blocked_record_is_not_expanded(self) -> None:
        claim_text = "We should revisit the coordinator note."
        claim_id = self._record_id("claim", claim_text)
        evidence_title = "Legal contract reference"
        evidence_id = self._record_id("evidence", evidence_title)
        evidence = new_evidence(
            vault=self.vault,
            title=evidence_title,
            source_type="document",
            source_uri="doc://legal/contract",
            actors=["Person B", "Counsel"],
            arena="work",
            compartments=["legal"],
            reliability="high",
            summary="A legal review mentioned a contract clause.",
            observed_facts=["A legal review mentioned a contract clause."],
            linked_claims=[],
            linked_episodes=[],
        )
        self._retarget_record(evidence.path, domain_primary="status", compartments=["legal"])

        new_claim(
            vault=self.vault,
            claim_text=claim_text,
            claim_class="observation",
            owner="user",
            status="active",
            confidence=0.6,
            supporting_evidence=[],
            contradicting_evidence=[],
            linked_patterns=[],
            arena="work",
            summary="A coordination note that points to the legal artifact.",
        )
        self._rebuild()

        result = retrieve_context("revisit the coordinator note", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == claim_id for item in result.direct_loaded))
        self.assertFalse(any(item.id == evidence_id for item in result.loaded))
        self.assertTrue(any(item.id == evidence_id for item in result.rejected))
        blocked_item = next(item for item in result.rejected if item.id == evidence_id)
        self.assertIn("compartment_blocked", blocked_item.reason)

        context = assemble_context("revisit the coordinator note", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertIn("## Rejected By Compartment", context)

    def test_cross_domain_expansion_is_blocked_without_justification(self) -> None:
        evidence_title = "Rollout request"
        evidence_id = self._record_id("evidence", evidence_title)
        claim_text = "Person A may be setting me up as a scapegoat."
        claim_id = self._record_id("claim", claim_text)
        evidence = new_evidence(
            vault=self.vault,
            title=evidence_title,
            source_type="email",
            source_uri="mail://thread/456",
            actors=["Person A", "Person B"],
            arena="work",
            reliability="high",
            summary="Person A asked Person B to present the rollout plan to management.",
            observed_facts=["Person A asked Person B to present the rollout plan to management."],
            linked_claims=[claim_id],
            linked_episodes=[],
        )
        self._retarget_record(evidence.path, domain_primary="work")

        claim = new_claim(
            vault=self.vault,
            claim_text=claim_text,
            claim_class="motive_hypothesis",
            owner="user",
            status="disputed",
            confidence=0.3,
            supporting_evidence=[],
            contradicting_evidence=[],
            linked_patterns=[],
            arena="status",
            summary="A cautious interpersonal reading.",
        )
        self._retarget_record(claim.path, domain_primary="status")
        self._rebuild()

        blocked = retrieve_context("project rollout request", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == evidence_id for item in blocked.direct_loaded))
        self.assertFalse(any(item.id == claim_id for item in blocked.loaded))
        self.assertTrue(any(item.id == claim_id for item in blocked.graph_blocked))
        self.assertTrue(any("cross_domain" in item.reason for item in blocked.graph_blocked))

        justified = retrieve_context("work relationship project rollout request", domain="work", vault=self.vault, db_path=self.db_path)
        self.assertTrue(any(item.id == claim_id for item in justified.expanded_loaded))
        claim_item = next(item for item in justified.expanded_loaded if item.id == claim_id)
        self.assertEqual(claim_item.expansion_source, evidence_id)
        self.assertIn("linked_claims", claim_item.expansion_reason)


if __name__ == "__main__":
    unittest.main()
