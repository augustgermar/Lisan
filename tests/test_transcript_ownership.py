"""Gate: the capture observer never appends to the transcript.

The 2026-07-12 defect class: the conversation layer appends both sides of
an exchange at receive time; the observer pipeline appended the user turn
AGAIN when its background job ran minutes later. The append-time dedup
guard only inspects the most recent same-speaker turn, so any multi-
message sequence (the Chrysalis session arrived as six chunks) put every
turn into the ground-truth transcript twice. Ownership is the fix: the
conversation layer writes, the observer only reads.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.memory_pipeline import run_memory_pipeline
from lisan.tools.transcripts import append_transcript, transcript_path_for


def _skip_listener(*args, **kwargs):
    return {
        "worth_remembering": False,
        "mode": "skip",
        "action": "skip",
        "reason": "test stub",
        "memory_events": [],
    }


class ObserverTranscriptOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.conv = "telegram-42-2026-07-12"

    def tearDown(self):
        self.tmp.cleanup()

    def _observe(self, text: str):
        with patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", _skip_listener):
            return run_memory_pipeline(
                vault=self.vault,
                text=text,
                conversation_id=self.conv,
                observed_response="Understood, recorded.",
            )

    def test_observer_does_not_append_the_user_turn_again(self):
        path = append_transcript(vault=self.vault, conversation_id=self.conv, speaker="USER", text="chunk one")
        append_transcript(vault=self.vault, conversation_id=self.conv, speaker="LISAN", text="reply one")
        before = path.read_text(encoding="utf-8")
        result = self._observe("chunk one")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(result.transcript_path, path)

    def test_interleaved_chunks_stay_single_copies(self):
        # Six chunks land back-to-back (receive-time appends), then their
        # observer jobs run late, in order. Every chunk must appear exactly
        # once — this is the exact shape the dedup guard missed.
        chunks = [f"chrysalis chunk {n}" for n in range(1, 7)]
        for chunk in chunks:
            append_transcript(vault=self.vault, conversation_id=self.conv, speaker="USER", text=chunk)
        for chunk in chunks:
            self._observe(chunk)
        content = transcript_path_for(self.vault).read_text(encoding="utf-8")
        for chunk in chunks:
            self.assertEqual(content.count(chunk), 1, f"{chunk!r} duplicated")

    def test_legacy_capture_path_still_appends(self):
        with patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", _skip_listener):
            result = run_memory_pipeline(
                vault=self.vault,
                text="a turn arriving through capture_text",
                conversation_id=self.conv,
            )
        content = result.transcript_path.read_text(encoding="utf-8")
        self.assertIn("a turn arriving through capture_text", content)


if __name__ == "__main__":
    unittest.main()
