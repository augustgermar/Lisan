"""Gate: a writer that can't produce schema-valid output must fail loudly.

The 2026-07-12 defect class: the Chrysalis session's open loops and
decisions were acknowledged in the reply ("I have all seven open items
logged") but never written — the writer's response failed the schema and
the deterministic fallback silently stood in, producing a hollow record.
With ``parse_error_mode="raise"`` the capture job fails instead, and the
queue's retry + escalation ladder owns the failure.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.agents.base import SchemaParseError
from lisan.agents.writer import WriterAgent
from lisan.paths import ensure_repo_layout, vault_root
from lisan.providers.base import LLMResponse


class _ProseLLM:
    """A provider that answers, but never in the required schema."""

    def complete(self, prompt, **kwargs) -> LLMResponse:
        return LLMResponse(
            text="Certainly! Here's a summary of what I noticed in this turn...",
            provider="stub",
            model="stub",
        )


class WriterStrictParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self) -> WriterAgent:
        agent = WriterAgent(vault=self.vault, config={})
        agent.llm = _ProseLLM()
        return agent

    def test_raise_mode_raises_instead_of_hollow_fallback(self):
        with self.assertRaises(SchemaParseError) as err:
            self._agent().run_json("the user said something worth remembering", parse_error_mode="raise")
        self.assertIn("writer", str(err.exception))

    def test_default_mode_still_falls_back(self):
        # Interactive callers keep the degraded-but-alive behavior unless
        # they opt in; only the background capture path demands strictness.
        out = self._agent().run_json("the user said something worth remembering")
        self.assertIsInstance(out, dict)

    def test_pipeline_writer_calls_opt_into_strict_parse(self):
        import inspect

        from lisan.tools import memory_pipeline

        source = inspect.getsource(memory_pipeline)
        self.assertIn('"parse_error_mode": "raise"', source)


class _ArtifactsOnlyLLM:
    """A provider that obeys the artifacts prompt: arrays only, no core keys."""

    def complete(self, prompt, **kwargs) -> LLMResponse:
        return LLMResponse(
            text=(
                '{"entities_to_create": [{"name": "Wren", "kind": "person", '
                '"summary": "A friend", "confidence_basis": "named in turn"}], '
                '"open_loops_to_create": [], "decisions_to_create": [], '
                '"behavioral_contracts": [], "state_updates": [], "evidence_to_create": []}'
            ),
            provider="stub",
            model="stub",
        )


class ArtifactsPassSchemaTests(unittest.TestCase):
    """The 2026-07-05..15 record-loss root cause: the artifacts prompt forbids
    the exact keys the writer_output schema requires, so every response that
    OBEYED the prompt failed validation and fell back to a hollow record.
    The artifacts pass must validate against its own schema."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_obedient_artifacts_response_is_accepted(self):
        agent = WriterAgent(vault=self.vault, config={})
        agent.llm = _ArtifactsOnlyLLM()
        out = agent.run_json(
            "a turn naming Wren", task="episode_artifacts", parse_error_mode="raise",
        )
        self.assertEqual(out["entities_to_create"][0]["name"], "Wren")

    def test_core_pass_still_requires_the_core_keys(self):
        agent = WriterAgent(vault=self.vault, config={})
        agent.llm = _ArtifactsOnlyLLM()
        with self.assertRaises(SchemaParseError):
            agent.run_json("a turn naming Wren", task="episode_core", parse_error_mode="raise")

    def test_degenerate_artifacts_response_still_fails(self):
        agent = WriterAgent(vault=self.vault, config={})
        agent.llm = _ProseLLM()
        with self.assertRaises(SchemaParseError):
            agent.run_json("a turn", task="episode_artifacts", parse_error_mode="raise")


if __name__ == "__main__":
    unittest.main()
