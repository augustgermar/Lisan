"""Behavioral-contract fast lane tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class BehavioralContractTests(unittest.TestCase):
    """Vellum borrow #6: standing instructions take effect by the next turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault / "primer").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_contract_lands_in_operating_style_dated_and_deduped(self):
        from lisan.tools.record_fanout import apply_behavioral_contracts

        writer = {"behavioral_contracts": ["Stop using bullet points in replies."]}
        n = apply_behavioral_contracts(self.vault, writer, source_ref="drafts/d1.md")
        self.assertEqual(n, 1)
        text = (self.vault / "primer" / "operating-style.md").read_text(encoding="utf-8")
        self.assertIn("Standing instructions (captured live)", text)
        self.assertIn("Stop using bullet points", text)
        self.assertIn("drafts/d1.md", text)
        # dedup: same instruction (differently punctuated) never doubles
        again = apply_behavioral_contracts(self.vault, {"behavioral_contracts": ["stop using bullet points in replies"]})
        self.assertEqual(again, 0)

    def test_empty_and_missing_are_noops(self):
        from lisan.tools.record_fanout import apply_behavioral_contracts

        self.assertEqual(apply_behavioral_contracts(self.vault, {}), 0)
        self.assertFalse((self.vault / "primer" / "operating-style.md").exists())

    def test_injected_into_conversation_owner_profile(self):
        from lisan.tools.conversation import _owner_profile
        from lisan.tools.record_fanout import apply_behavioral_contracts

        apply_behavioral_contracts(self.vault, {"behavioral_contracts": ["Always give dates in ISO format."]})
        profile = _owner_profile(self.vault)
        self.assertIn("Always give dates in ISO format", profile)
        self.assertIn("honor these every turn", profile)


if __name__ == "__main__":
    unittest.main()
