"""Tests for Finding #12 (cross-conversation preamble plumbing)."""

from __future__ import annotations

import inspect
import unittest

from lisan.agents.assembler import AssemblerAgent


class AssemblerForwardsConversationIdTests(unittest.TestCase):
    def test_run_accepts_conversation_id_kwarg(self) -> None:
        sig = inspect.signature(AssemblerAgent.run)
        self.assertIn("conversation_id", sig.parameters)

    def test_run_accepts_domain_and_arena_kwargs(self) -> None:
        sig = inspect.signature(AssemblerAgent.run)
        self.assertIn("domain", sig.parameters)
        self.assertIn("arena", sig.parameters)


if __name__ == "__main__":
    unittest.main()
