from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.agents.advice import AdviceAgent
from lisan.agents.interlocutor import InterlocutorAgent
from lisan.agents.router import RouterAgent


def _vault_with_assistant_name(name: str = "Dabiku") -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "primer").mkdir(parents=True, exist_ok=True)
    (tmp / "primer" / "identity-core.md").write_text(
        f"""---
principal:
  name: "August Germar"
  aliases: ["August"]
assistant:
  name: "{name}"
  canonical_name: "{name}"
  aliases: ["{name}"]
deixis_frame: |
  I / me / {name} = the assistant.
  you / your = August.
---

# Identity Core
""",
        encoding="utf-8",
    )
    return tmp


class PromptIdentityTests(unittest.TestCase):
    def test_interlocutor_prompt_uses_instance_name(self) -> None:
        vault = _vault_with_assistant_name("Dabiku")
        prompt = InterlocutorAgent(vault=vault).prompt()
        self.assertIn("You are Dabiku", prompt)
        self.assertIn('If asked your name, answer "Dabiku"', prompt)
        self.assertNotIn("You are Lisan", prompt)

    def test_advice_prompt_uses_instance_name(self) -> None:
        vault = _vault_with_assistant_name("Dabiku")
        prompt = AdviceAgent(vault=vault).prompt()
        self.assertIn("You are Dabiku in general-assistant mode.", prompt)
        self.assertIn('If asked your name, answer "Dabiku"', prompt)
        self.assertNotIn("You are Lisan", prompt)

    def test_router_prompt_uses_instance_name(self) -> None:
        vault = _vault_with_assistant_name("Dabiku")
        prompt = RouterAgent(vault=vault).prompt()
        self.assertIn("You are the Dabiku mode router.", prompt)
        self.assertNotIn("You are the Lisan mode router.", prompt)

    def test_writer_decision_prompt_requires_specific_confidence_basis(self) -> None:
        prompt = Path("/Users/august/Code/erasmus/lisan/prompts/writer_decision_v1.md").read_text(encoding="utf-8")
        self.assertIn('Do **not** use the generic fallback "Auto-extracted from conversation."', prompt)
        self.assertIn("If the basis is genuinely unclear", prompt)

    def test_writer_state_prompt_uses_kind_schema(self) -> None:
        prompt = Path("/Users/august/Code/erasmus/lisan/prompts/writer_state_v1.md").read_text(encoding="utf-8")
        self.assertIn("entities_to_create`: array of `{name, kind, summary, confidence_basis}`", prompt)
        self.assertIn("`kind` describes what the entity is, not the turn type:", prompt)
        self.assertIn("When the user introduces someone by name", prompt)
        self.assertIn("Write a meaningful summary from the local context", prompt)

    def test_writer_episode_core_prompt_mentions_kind(self) -> None:
        prompt = Path("/Users/august/Code/erasmus/lisan/prompts/writer_episode_core_v1.md").read_text(encoding="utf-8")
        self.assertIn("metadata as `kind` (not `subtype`)", prompt)
