"""Regression tests for reference resolution adapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.providers.embeddings import EmbeddingProvider
from lisan.tools.record_factory import upsert_state
from lisan.tools.record_fanout import fanout_decisions, fanout_open_loops


def _write_record(path: Path, frontmatter: dict[str, object], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_markdown(frontmatter, body), encoding="utf-8")


def _fake_embed_text(self, text: str) -> list[float]:
    lowered = str(text).lower()
    if "jonah" in lowered:
        return [1.0, 0.0]
    if any(term in lowered for term in ("budget", "balance", "tax", "money", "fund", "aside")):
        return [0.0, 1.0]
    return [0.0, 0.0]


class DecisionSupersessionTests(unittest.TestCase):
    def test_reversal_supersedes_matching_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            active_one = vault / "decisions" / "2026-06-20-keep-jonah-direct.md"
            active_two = vault / "decisions" / "2026-06-20-use-full-names-for-jonah.md"
            unrelated_path = vault / "decisions" / "2026-06-20-keep-budget-on-track.md"

            _write_record(
                active_one,
                {
                    "id": "decision.keep-jonah-direct",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Keep Jonah named directly",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Keep Jonah named directly\n\nKeep Jonah named directly.\n",
            )
            _write_record(
                active_two,
                {
                    "id": "decision.use-full-names-for-jonah",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Use full names for Jonah",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Use full names for Jonah\n\nUse full names for Jonah.\n",
            )
            _write_record(
                unrelated_path,
                {
                    "id": "decision.keep-budget-on-track",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Keep the budget on track",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Keep the budget on track\n\nKeep the budget on track.\n",
            )

            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_decisions(
                    vault,
                    {
                        "decisions_to_create": [
                            {
                                "title": "Use full names for Jonah",
                                "summary": "Use full names for Jonah",
                                "significance": "medium",
                            }
                        ],
                    },
                    draft_rel="drafts/test.md",
                    source_text="I changed my mind and want to keep Jonah named directly.",
                )

            self.assertEqual(load_markdown(active_one).frontmatter["status"], "superseded")
            self.assertEqual(load_markdown(active_two).frontmatter["status"], "superseded")
            self.assertEqual(load_markdown(unrelated_path).frontmatter["status"], "active")
            self.assertEqual(load_markdown(active_one).frontmatter["superseded_by"], "decision.use-full-names-for-jonah")
            self.assertEqual(load_markdown(active_two).frontmatter["superseded_by"], "decision.use-full-names-for-jonah")

            docs = sorted((vault / "decisions").glob("*.md"))
            self.assertEqual(len(docs), 4)
            created_docs = [path for path in docs if path not in {active_one, active_two, unrelated_path}]
            self.assertEqual(len(created_docs), 1)
            self.assertEqual(load_markdown(created_docs[0]).frontmatter["status"], "active")

    def test_reinstatement_reactivates_prior_decision_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            prior_path = vault / "decisions" / "2026-06-20-keep-jonah-direct.md"
            intermediate_path = vault / "decisions" / "2026-06-20-use-full-names-for-jonah.md"

            _write_record(
                prior_path,
                {
                    "id": "decision.keep-jonah-direct",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "superseded",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Keep Jonah named directly",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "decision.use-full-names-for-jonah",
                },
                "# Keep Jonah named directly\n\nKeep Jonah named directly.\n",
            )
            _write_record(
                intermediate_path,
                {
                    "id": "decision.use-full-names-for-jonah",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Use full names for Jonah",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Use full names for Jonah\n\nUse full names for Jonah.\n",
            )

            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_decisions(
                    vault,
                    {
                        "decisions_to_create": [
                            {
                                "title": "Going back to keeping Jonah named directly",
                                "summary": "Going back to keeping Jonah named directly",
                                "significance": "medium",
                            }
                        ],
                    },
                    draft_rel="drafts/test.md",
                    source_text="I am going back to keeping Jonah named directly.",
                )

            self.assertEqual(load_markdown(prior_path).frontmatter["status"], "active")
            self.assertEqual(load_markdown(prior_path).frontmatter["superseded_by"], "")
            self.assertEqual(load_markdown(intermediate_path).frontmatter["status"], "superseded")
            self.assertEqual(load_markdown(intermediate_path).frontmatter["superseded_by"], "decision.keep-jonah-direct")
            self.assertEqual(len(list((vault / "decisions").glob("*.md"))), 2)

    def test_vague_reversal_does_not_supersede_unrelated_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            budget_path = vault / "decisions" / "2026-06-20-keep-budget-on-track.md"
            _write_record(
                budget_path,
                {
                    "id": "decision.keep-budget-on-track",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Keep the budget on track",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Keep the budget on track\n\nKeep the budget on track.\n",
            )

            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_decisions(
                    vault,
                    {
                        "decisions_to_create": [
                            {
                                "title": "Change the menu",
                                "summary": "Change the menu",
                                "significance": "low",
                            }
                        ],
                    },
                    draft_rel="drafts/test.md",
                    source_text="I changed my mind about the menu.",
                )

            self.assertEqual(load_markdown(budget_path).frontmatter["status"], "active")

    def test_long_source_text_does_not_supersede_priya_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            priya_path = vault / "decisions" / "2026-06-20-keep-priya-posted.md"
            tax_path = vault / "decisions" / "2026-06-20-finalize-tax-reserve.md"

            _write_record(
                priya_path,
                {
                    "id": "decision.keep-priya-posted",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Keep Priya posted on the note",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Keep Priya posted on the note\n\nKeep Priya posted on the note.\n",
            )
            _write_record(
                tax_path,
                {
                    "id": "decision.finalize-tax-reserve",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Finalize the tax reserve",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "revisit_after": "2026-06-20",
                    "revisit_conditions": [],
                    "alternatives_considered": [],
                    "supersedes": [],
                    "superseded_by": "",
                },
                "# Finalize the tax reserve\n\nFinalize the tax reserve.\n",
            )

            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_decisions(
                    vault,
                    {
                        "decisions_to_create": [
                            {
                                "title": "Finalize the tax reserve",
                                "summary": "Finalize the tax reserve",
                                "significance": "medium",
                            }
                        ],
                    },
                    draft_rel="drafts/test.md",
                    source_text="I changed my mind and want to finalize the tax reserve; I also want to keep Priya posted in a follow-up note.",
                )

            self.assertEqual(load_markdown(priya_path).frontmatter["status"], "active")
            self.assertEqual(load_markdown(tax_path).frontmatter["status"], "superseded")


class OpenLoopClosureTests(unittest.TestCase):
    def test_synonym_completion_closes_matching_open_loop_and_not_unrelated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            budget_path = vault / "open_loops" / "2026-06-20-set-aside-money-for-the-budget.md"
            unrelated_path = vault / "open_loops" / "2026-06-20-email-the-invitation.md"

            _write_record(
                budget_path,
                {
                    "id": "open_loop.set-aside-money-for-the-budget",
                    "type": "open_loop",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "low",
                    "domain_primary": "financial",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Set aside money for the budget",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "priority": "medium",
                    "owner": "user",
                    "next_action": "Set aside money for the budget",
                    "blocked_by": None,
                    "resolved_by": "",
                    "resolved_note": "",
                    "resolved_at": "",
                },
                "# Set aside money for the budget\n\n## Next Action\n\nSet aside money for the budget\n",
            )
            _write_record(
                unrelated_path,
                {
                    "id": "open_loop.email-the-invitation",
                    "type": "open_loop",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "low",
                    "domain_primary": "relational",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Email the invitation",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "priority": "medium",
                    "owner": "user",
                    "next_action": "Email the invitation",
                    "blocked_by": None,
                    "resolved_by": "",
                    "resolved_note": "",
                    "resolved_at": "",
                },
                "# Email the invitation\n\n## Next Action\n\nEmail the invitation\n",
            )

            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_open_loops(
                    vault,
                    {"open_loops_to_create": []},
                    draft_rel="drafts/test.md",
                    source_text="I separated the tax balance.",
                )

            self.assertEqual(load_markdown(budget_path).frontmatter["status"], "resolved")
            self.assertEqual(load_markdown(budget_path).frontmatter["resolved_by"], "drafts/test.md")
            self.assertEqual(load_markdown(budget_path).frontmatter["resolved_note"], "Resolved by drafts/test.md")
            self.assertEqual(load_markdown(unrelated_path).frontmatter["status"], "active")

            vague_path = vault / "open_loops" / "2026-06-20-call-the-plumber.md"
            _write_record(
                vague_path,
                {
                    "id": "open_loop.call-the-plumber",
                    "type": "open_loop",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "low",
                    "domain_primary": "cross_arena",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Call the plumber",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "priority": "medium",
                    "owner": "user",
                    "next_action": "Call the plumber",
                    "blocked_by": None,
                    "resolved_by": "",
                    "resolved_note": "",
                    "resolved_at": "",
                },
                "# Call the plumber\n\n## Next Action\n\nCall the plumber\n",
            )
            with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
                fanout_open_loops(
                    vault,
                    {"open_loops_to_create": []},
                    draft_rel="drafts/test-2.md",
                    source_text="I mailed the invitation.",
                )
            self.assertEqual(load_markdown(vague_path).frontmatter["status"], "active")


class StateMergeTests(unittest.TestCase):
    def test_recent_state_summaries_accumulate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            upsert_state(
                vault,
                "work",
                "Daniel acknowledged the plan.",
                confidence="low",
            )
            upsert_state(
                vault,
                "work",
                "Elena bypassed design review.",
                confidence="low",
            )

            path = vault / "state" / "work-current.md"
            fm = load_markdown(path).frontmatter
            recent = fm["recent_summaries"]
            self.assertEqual(len(recent), 2)
            self.assertIn("Daniel acknowledged the plan.", fm["summary"])
            self.assertIn("Elena bypassed design review.", fm["summary"])


if __name__ == "__main__":
    unittest.main()
