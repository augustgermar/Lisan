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

    def test_skip_turn_answers_question_via_interlocutor(self) -> None:
        """FIX B: a recall turn answers the question from the retrieved records
        via the Interlocutor, not a raw summary dump. FIX A: role tokens in the
        records are rendered to the user-facing audience before the model sees
        them, and never leak into the answer."""
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
                SimpleNamespace(id="decision.one", type="decision",
                                summary="{{principal}} decided to ship the beta on Friday."),
                SimpleNamespace(id="decision.one", type="decision",
                                summary="{{principal}} decided to ship the beta on Friday."),
                SimpleNamespace(id="episode.two", type="episode",
                                summary="Monica warned about launch risk."),
            ]
        )

        captured: dict = {}

        def fake_run_json(self, user_input, **kwargs):
            captured["user_input"] = user_input
            return {"response": "You decided to ship the beta on Friday; Monica flagged launch risk."}

        with (
            patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
            patch("lisan.tools.memory_pipeline.retrieve_context", return_value=retrieval_result),
            patch("lisan.tools.memory_pipeline.InterlocutorAgent.run_json", new=fake_run_json),
        ):
            result = capture_text(
                vault=self.vault,
                text="What did I decide about the beta launch?",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        self.assertEqual(result["action"], "skip")
        # The Interlocutor was actually invoked (previously the skip path was
        # listener-only and never reached a generation pass).
        self.assertIn("user_input", captured)
        # The records reached the answerer already deixis-rendered ("you"),
        # never as a raw {{principal}} token.
        self.assertNotIn("{{principal}}", captured["user_input"])
        self.assertIn("you decided to ship the beta on friday.", captured["user_input"].lower())
        # The response is the answer, not the old "Here's what I found" dump.
        self.assertEqual(
            result["response"],
            "You decided to ship the beta on Friday; Monica flagged launch risk.",
        )
        self.assertNotIn("Here's what I found", result["response"])
        self.assertNotIn("{{principal}}", result["response"])

    def test_skip_turn_falls_back_when_interlocutor_unavailable(self) -> None:
        """If the answerer errors, the recall turn still returns a rendered
        record list (no fabrication, no token leak) rather than failing."""
        listener = {
            "worth_remembering": False, "mode": "skip", "reason": ["retrieval question"],
            "memory_events": [], "action": "skip", "score": 1, "seed_score": 0,
            "narrative_score": 0, "memory_type": "skip",
        }
        retrieval_result = SimpleNamespace(
            loaded=[SimpleNamespace(id="decision.one", type="decision",
                                    summary="{{principal}} decided to ship the beta on Friday.")]
        )

        def boom(self, *a, **k):
            raise RuntimeError("provider down")

        with (
            patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
            patch("lisan.tools.memory_pipeline.retrieve_context", return_value=retrieval_result),
            patch("lisan.tools.memory_pipeline.InterlocutorAgent.run_json", new=boom),
        ):
            result = capture_text(
                vault=self.vault,
                text="What did I decide about the beta launch?",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        self.assertEqual(result["action"], "skip")
        self.assertIn("Here's what I found", result["response"])
        self.assertIn("you decided to ship the beta on Friday.", result["response"])
        self.assertNotIn("{{principal}}", result["response"])

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

    def test_skip_turn_farewell_gets_ack_without_recall_lookup(self) -> None:
        listener = {
            "worth_remembering": False,
            "mode": "skip",
            "reason": ["social closing"],
            "memory_events": [],
            "action": "skip",
            "score": 0,
            "seed_score": 0,
            "narrative_score": 0,
            "memory_type": "skip",
        }

        with (
            patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
            patch("lisan.tools.memory_pipeline.retrieve_context") as mock_retrieve,
            patch("lisan.tools.memory_pipeline.InterlocutorAgent.run_json") as mock_interlocutor,
        ):
            result = capture_text(
                vault=self.vault,
                text="ok thanks, heading out. later.",
                conversation_id="demo",
                queue_background=False,
                db_path=self.db_path,
            )

        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["response"], "Talk soon.")
        self.assertNotIn("didn't ask for a specific memory", result["response"].lower())
        mock_retrieve.assert_not_called()
        mock_interlocutor.assert_not_called()

    def test_skip_turn_short_ack_gets_ack_without_recall_lookup(self) -> None:
        listener = {
            "worth_remembering": False,
            "mode": "skip",
            "reason": ["short acknowledgment"],
            "memory_events": [],
            "action": "skip",
            "score": 0,
            "seed_score": 0,
            "narrative_score": 0,
            "memory_type": "skip",
        }

        for text in ("thanks!", "ok", "bye"):
            with self.subTest(text=text):
                with (
                    patch("lisan.tools.memory_pipeline.ListenerAgent.run_json", return_value=listener),
                    patch("lisan.tools.memory_pipeline.retrieve_context") as mock_retrieve,
                    patch("lisan.tools.memory_pipeline.InterlocutorAgent.run_json") as mock_interlocutor,
                ):
                    result = capture_text(
                        vault=self.vault,
                        text=text,
                        conversation_id="demo",
                        queue_background=False,
                        db_path=self.db_path,
                    )

                self.assertEqual(result["action"], "skip")
                self.assertIn(result["response"], {"Okay.", "Talk soon."})
                mock_retrieve.assert_not_called()
                mock_interlocutor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
