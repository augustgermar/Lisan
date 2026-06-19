from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.capture import capture_text


class SkipRetrievalResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_skip_turn_returns_retrieval_summary_response(self) -> None:
        listener = {
            "worth_remembering": False,
            "mode": "skip",
            "reason": ["retrieval question"],
            "memory_events": [],
            "action": "skip",
            "score": 1,
            "seed_score": 0,
            "narrative_score": 0,
            "memory_type": "skip",
        }
        retrieval_result = SimpleNamespace(
            loaded=[
                SimpleNamespace(id="decision.one", summary="You decided to ship the beta on Friday."),
                SimpleNamespace(id="decision.one", summary="You decided to ship the beta on Friday."),
                SimpleNamespace(id="episode.two", summary="Monica warned about launch risk."),
            ]
        )

        with (
            patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
            patch("lisan.tools.memory_pipeline.retrieve_context", return_value=retrieval_result),
        ):
            result = capture_text(
                vault=self.vault,
                text="What did I decide about the beta launch?",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        self.assertEqual(result["action"], "skip")
        self.assertIn("Here's what I found in your stored records:", result["response"])
        self.assertIn("You decided to ship the beta on Friday.", result["response"])
        self.assertIn("Monica warned about launch risk.", result["response"])

    def test_skip_turn_returns_explicit_empty_fallback(self) -> None:
        listener = {
            "worth_remembering": False,
            "mode": "skip",
            "reason": ["retrieval question"],
            "memory_events": [],
            "action": "skip",
            "score": 1,
            "seed_score": 0,
            "narrative_score": 0,
            "memory_type": "skip",
        }
        retrieval_result = SimpleNamespace(loaded=[])

        with (
            patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
            patch("lisan.tools.memory_pipeline.retrieve_context", return_value=retrieval_result),
        ):
            result = capture_text(
                vault=self.vault,
                text="What do you remember about my dentist appointment?",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        self.assertEqual(result["response"], "I don't have anything stored about that yet.")


if __name__ == "__main__":
    unittest.main()
