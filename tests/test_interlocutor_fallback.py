"""Tests for the interlocutor fallback (Findings #10 and #11)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.agents.interlocutor import InterlocutorAgent
from lisan.frontmatter import dump_markdown


def _make_agent(vault: Path) -> InterlocutorAgent:
    # Bypass full __init__ to avoid pulling in provider config during tests.
    agent = InterlocutorAgent.__new__(InterlocutorAgent)
    agent.vault = vault
    agent.config = {}
    agent.prompt_file = "interlocutor_v1"
    return agent


class InterlocutorFallbackContentTests(unittest.TestCase):
    """The fallback must never emit 'I need a little more detail to proceed.'"""

    def test_decision_mirrors_decisive_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            payload = json.dumps({
                "writer_summary": "send Marcus a written summary of the three incidents",
                "decisions": ["send Marcus a written summary of the three incidents"],
                "writer_questions": [],
            })
            result_text = agent.fallback_output(payload)
            result = json.loads(result_text)
            self.assertNotIn("I need a little more detail", result["response"])
            self.assertTrue(result["response"].startswith("Noted"),
                            f"Expected 'Noted...' prefix, got: {result['response']!r}")

    def test_summary_mirrored_when_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            payload = json.dumps({
                "writer_summary": "Marcus pulled the feature three days before sprint close.",
                "decisions": [],
                "writer_questions": [],
            })
            result = json.loads(agent.fallback_output(payload))
            self.assertNotIn("I need a little more detail", result["response"])
            self.assertIn("Marcus", result["response"])

    def test_empty_payload_does_not_emit_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            result = json.loads(agent.fallback_output("{}"))
            self.assertNotIn("I need a little more detail", result["response"])

    def test_directness_preference_changes_phrasing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            fm = {"directness": True}
            (vault / "primer" / "operating-style.md").write_text(
                dump_markdown(fm, "# Operating Style\n"),
                encoding="utf-8",
            )
            agent = _make_agent(vault)
            payload = json.dumps({
                "writer_summary": "the meeting got rescheduled to Friday",
                "decisions": [],
            })
            result = json.loads(agent.fallback_output(payload))
            self.assertTrue(result["response"].startswith("Heard"),
                            f"Direct style expected, got: {result['response']!r}")


class InterlocutorParseStrictnessTests(unittest.TestCase):
    """Finding #11: parse_output rejects dicts missing the 'response' field."""

    def test_dict_without_response_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            self.assertIsNone(agent.parse_output('{"text": "some prose"}'))

    def test_dict_with_blank_response_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            self.assertIsNone(agent.parse_output('{"response": "   "}'))

    def test_valid_response_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            agent = _make_agent(vault)
            parsed = agent.parse_output('{"response": "Heard that."}')
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["response"], "Heard that.")


if __name__ == "__main__":
    unittest.main()
