from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.chat_turns import classify_turn
from lisan.tools.transcripts import append_transcript


class FastPathContextTests(unittest.TestCase):
    """Canned fast-path replies are for fresh conversations only. Once a
    conversation is underway, short turns ("you pick", "go ahead") are the
    most context-dependent messages there are — they must reach the
    context-bearing pipeline. This is the production "you pick" thread-drop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _start_conversation(self, conversation_id: str):
        append_transcript(vault=self.vault, conversation_id=conversation_id, speaker="USER",
                          text="lets go through my obsidian files and ingest them")

    def test_fresh_conversation_keeps_cheap_bare_acks(self):
        # bare acknowledgments still fast-path; substantive questions do not.
        out = classify_turn("thanks", vault=self.vault, conversation_id="fresh-1")
        self.assertTrue(out.fast_path_used)
        self.assertIsNotNone(out.deterministic_response)

    def test_capability_question_reaches_agent_even_when_fresh(self):
        # "what can you do?" must use the capability model, never canned help.
        out = classify_turn("what can you do?", vault=self.vault, conversation_id="fresh-2")
        self.assertFalse(out.fast_path_used)
        self.assertIsNone(out.deterministic_response)

    def test_you_pick_mid_conversation_reaches_pipeline(self):
        self._start_conversation("conv-1")
        for text in ("you pick", "go ahead", "the first one", "sure, do that"):
            out = classify_turn(text, vault=self.vault, conversation_id="conv-1")
            self.assertIsNone(out.deterministic_response, f"canned reply for {text!r}")
            self.assertFalse(out.fast_path_used, f"fast path for {text!r}")

    def test_status_question_mid_conversation_reaches_pipeline(self):
        self._start_conversation("conv-2")
        out = classify_turn("how are you", vault=self.vault, conversation_id="conv-2")
        self.assertFalse(out.fast_path_used)

    def test_identity_stays_canned_even_mid_conversation(self):
        self._start_conversation("conv-3")
        out = classify_turn("what is your name?", vault=self.vault, conversation_id="conv-3")
        self.assertTrue(out.fast_path_used)


if __name__ == "__main__":
    unittest.main()
