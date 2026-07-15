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


if __name__ == "__main__":
    unittest.main()
