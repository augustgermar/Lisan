"""Regression tests for reference resolution adapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.tools.record_factory import new_decision, upsert_state
from lisan.tools.record_fanout import fanout_decisions, fanout_open_loops


def _write_record(path: Path, frontmatter: dict[str, object], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_markdown(frontmatter, body), encoding="utf-8")


class DecisionSupersessionTests(unittest.TestCase):
    def test_reversal_supersedes_prior_active_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            old_path = vault / "decisions" / "2026-06-20-switch-writer-to-local-provider.md"
            _write_record(
                old_path,
                {
                    "id": "decision.switch-writer-to-local-provider",
                    "type": "decision",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "medium",
                    "domain_primary": "work",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Switch writer to local provider",
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
                "# Switch writer to local provider\n\nSwitch writer to local provider.\n",
            )

            fanout_decisions(
                vault,
                {
                    "decisions_to_create": [
                        {
                            "title": "Switch writer to codex provider",
                            "summary": "Switch writer to codex provider",
                            "significance": "medium",
                        }
                    ],
                },
                draft_rel="drafts/test.md",
                source_text="I changed my mind and will switch to codex provider instead.",
            )

            old_fm = load_markdown(old_path).frontmatter
            self.assertEqual(old_fm["status"], "superseded")
            self.assertEqual(old_fm["superseded_by"], "decision.switch-writer-to-codex-provider")

            new_docs = list((vault / "decisions").glob("*switch-writer-to-codex-provider.md"))
            self.assertEqual(len(new_docs), 1)
            new_fm = load_markdown(new_docs[0]).frontmatter
            self.assertIn("decision.switch-writer-to-local-provider", new_fm["supersedes"])
            self.assertIn("decision.switch-writer-to-local-provider", new_fm["links"])


class OpenLoopClosureTests(unittest.TestCase):
    def test_completion_closes_matching_open_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            old_path = vault / "open_loops" / "2026-06-20-tell-linda-about-the-boundary.md"
            _write_record(
                old_path,
                {
                    "id": "open_loop.tell-linda-about-the-boundary",
                    "type": "open_loop",
                    "created": "2026-06-20",
                    "updated": "2026-06-20",
                    "status": "active",
                    "significance": "low",
                    "domain_primary": "relational",
                    "domain_secondary": [],
                    "privacy": "personal",
                    "disclosure": "private",
                    "summary": "Tell Linda about the boundary",
                    "links": [],
                    "confidence": "low",
                    "confidence_basis": "seed",
                    "last_confirmed": "2026-06-20",
                    "review_after": "2026-06-20",
                    "priority": "medium",
                    "owner": "user",
                    "next_action": "Tell Linda about the boundary",
                    "blocked_by": None,
                    "resolved_by": "",
                    "resolved_note": "",
                    "resolved_at": "",
                },
                "# Tell Linda about the boundary\n\n## Next Action\n\nTell Linda about the boundary\n",
            )

            fanout_open_loops(
                vault,
                {"open_loops_to_create": []},
                draft_rel="drafts/test.md",
                source_text="I told Linda about the boundary yesterday.",
            )

            fm = load_markdown(old_path).frontmatter
            self.assertEqual(fm["status"], "resolved")
            self.assertEqual(fm["resolved_by"], "drafts/test.md")
            self.assertEqual(fm["resolved_note"], "Resolved by drafts/test.md")
            self.assertEqual(fm["resolved_at"], fm["updated"])


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
