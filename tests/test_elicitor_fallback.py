"""Tests for the elicitor fallback (Finding #9)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.agents.elicitor import ElicitorAgent


def _make_agent(vault: Path) -> ElicitorAgent:
    agent = ElicitorAgent.__new__(ElicitorAgent)
    agent.vault = vault
    agent.config = {}
    agent.prompt_file = "elicitor_v1"
    agent._attempted_prose_recovery = True  # short-circuit the LLM recovery path
    return agent


def _seed_primer(vault: Path, identity_text: str) -> None:
    (vault / "primer").mkdir(parents=True, exist_ok=True)
    (vault / "primer" / "identity.md").write_text(identity_text, encoding="utf-8")


class ElicitorEntitySelectionTests(unittest.TestCase):
    def test_unknown_capitalized_word_not_emitted_as_entity(self) -> None:
        """Bare 'Strategically' or 'Friday' must not surface as an entity."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nNadia Okonkwo, engineer.\n")
            agent = _make_agent(vault)
            entities = agent._entities(
                "Strategically, I think Marcus has decided not to escalate this Friday."
            )
            self.assertNotIn("Strategically", entities)
            self.assertNotIn("Friday", entities)

    def test_primer_known_name_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault,
                "# Identity\n\nNadia Okonkwo, engineer.\nWorks with Marcus Webb.\n")
            agent = _make_agent(vault)
            entities = agent._entities(
                "Marcus pulled the feature again three days before sprint close."
            )
            self.assertIn("Marcus", entities)

    def test_august_in_primer_is_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nAugust Germar, developer.\n")
            agent = _make_agent(vault)
            entities = agent._entities("August asked about the new feature today.")
            self.assertIn("August", entities)


class ElicitorOpenerRotationTests(unittest.TestCase):
    def test_no_generic_tell_me_more_for_unknown_capitalized_word(self) -> None:
        """Previously: 'Strategically' would be picked as the noun, producing
        'Tell me more about Strategically.' That cannot happen anymore."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nNadia Okonkwo, engineer.\n")
            agent = _make_agent(vault)
            output = json.loads(agent.fallback_output(
                "Strategically, I think Marcus has decided not to escalate this Friday."
            ))
            response = output["response"]
            self.assertNotIn("Strategically", response)
            self.assertNotIn("Friday", response)

    def test_fallback_response_anchors_on_primer_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault,
                "# Identity\n\nNadia Okonkwo, engineer.\nWorks with Marcus Webb.\n")
            agent = _make_agent(vault)
            output = json.loads(agent.fallback_output(
                "Marcus pulled the feature again three days before sprint close."
            ))
            self.assertIn("Marcus", output["response"])


if __name__ == "__main__":
    unittest.main()
