"""Tests for the turn-routing cascade in memory_pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lisan.tools.memory_pipeline import RoutingContext, route_turn


def _ctx(
    vault: Path,
    *,
    text: str,
    listener: dict[str, object],
    mode_status: str = "open",
    turn_count: int = 0,
    conversation_id: str | None = None,
) -> RoutingContext:
    return RoutingContext(
        text=text,
        listener=listener,
        prior_state=SimpleNamespace(mode_status=mode_status, turn_count=turn_count),
        conversation_id=conversation_id,
        vault=vault,
    )


class RoutingCascadeTests(unittest.TestCase):
    def test_correction_override_sets_memory_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = route_turn(
                _ctx(
                    vault,
                    text="Actually, I was wrong about Marcus.",
                    listener={
                        "action": "full",
                        "mode": "extraction",
                        "memory_type": "episode",
                        "seed_score": 0,
                        "narrative_score": 0,
                        "reason": [],
                    },
                )
            )
            self.assertEqual(result.listener["memory_type"], "correction")
            self.assertIn("correction_override", result.applied_overrides)

    def test_skip_promotes_to_elicitor_mid_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = route_turn(
                _ctx(
                    vault,
                    text="Just a normal update.",
                    listener={
                        "action": "skip",
                        "mode": "skip",
                        "memory_type": "skip",
                        "seed_score": 1,
                        "narrative_score": 0,
                        "reason": [],
                    },
                    turn_count=3,
                )
            )
            self.assertEqual(result.action, "lightweight")
            self.assertEqual(result.mode, "elicitor")
            self.assertIn("never_skip_mid_conversation", result.applied_overrides)

    def test_turn1_distress_promotes_extraction_to_elicitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = route_turn(
                _ctx(
                    vault,
                    text="I'm worried about Marcus and need help.",
                    listener={
                        "action": "full",
                        "mode": "extraction",
                        "memory_type": "episode",
                        "seed_score": 0,
                        "narrative_score": 0,
                        "reason": ["affect signal"],
                    },
                    conversation_id="demo",
                )
            )
            self.assertEqual(result.mode, "elicitor")
            self.assertIn("turn1_elicitor_preference", result.applied_overrides)

    def test_complete_statement_overrides_elicitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = route_turn(
                _ctx(
                    vault,
                    text="Marcus got the promotion.",
                    listener={
                        "action": "full",
                        "mode": "elicitor",
                        "memory_type": "episode",
                        "seed_score": 0,
                        "narrative_score": 6,
                        "reason": [],
                    },
                )
            )
            self.assertEqual(result.mode, "extraction")
            self.assertIn("narratively_complete_extraction", result.applied_overrides)

    def test_skip_promotion_can_still_end_in_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            result = route_turn(
                _ctx(
                    vault,
                    text="Honestly, I'm not sure what to do but Marcus got the promotion.",
                    listener={
                        "action": "skip",
                        "mode": "skip",
                        "memory_type": "skip",
                        "seed_score": 1,
                        "narrative_score": 8,
                        "reason": [],
                    },
                    turn_count=2,
                )
            )
            self.assertEqual(result.action, "lightweight")
            self.assertEqual(result.mode, "extraction")
            self.assertEqual(
                result.applied_overrides,
                ("never_skip_mid_conversation", "narratively_complete_extraction"),
            )


class ActionRequestOverrideTests(unittest.TestCase):
    """Explicit action requests must leave the elicitor (which cannot act)
    for the tool-bearing extraction path — the production Obsidian-ingestion
    dodge came from this gap."""

    def _route(self, vault: Path, text: str):
        return route_turn(
            _ctx(
                vault,
                text=text,
                listener={
                    "action": "lightweight",
                    "mode": "elicitor",
                    "memory_type": "episode",
                    "seed_score": 1,
                    "narrative_score": 0,
                    "reason": [],
                },
            )
        )

    def test_path_plus_verb_forces_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._route(
                Path(tmp),
                "check out the files at /Users/august/Documents/Vault01/ and see if you can absorb data from them. include subdirectories",
            )
            self.assertEqual(result.mode, "extraction")
            self.assertIn("action_request_extraction", result.applied_overrides)

    def test_file_object_plus_file_verb_forces_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._route(Path(tmp), "can you show me the files on my desktop")
            self.assertEqual(result.mode, "extraction")

    def test_ingestion_request_forces_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._route(Path(tmp), "I would like you to ingest my obsidian notes")
            self.assertEqual(result.mode, "extraction")

    def test_skip_classified_action_request_still_acts(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = route_turn(
                _ctx(
                    Path(tmp),
                    text="look at the files in this folder and absorb whatever is useful: /Users/me/notes",
                    listener={
                        "action": "skip",
                        "mode": "skip",
                        "memory_type": "skip",
                        "seed_score": 0,
                        "narrative_score": 0,
                        "reason": [],
                    },
                )
            )
            self.assertEqual(result.action, "lightweight")
            self.assertEqual(result.mode, "extraction")
            self.assertIn("action_request_never_skip", result.applied_overrides)

    def test_narrative_turns_stay_with_the_elicitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in (
                "I went for a run this morning and felt great",
                "I read a wonderful book about lighthouses",
                "Maya showed me her drawing of Saturn",
                "we had to move my mom into the new place this weekend",
            ):
                result = self._route(Path(tmp), text)
                self.assertEqual(result.mode, "elicitor", f"misrouted narrative turn: {text!r}")
                self.assertNotIn("action_request_extraction", result.applied_overrides)


class SelfStateQueryTests(unittest.TestCase):
    def test_detects_state_questions(self):
        from lisan.tools.memory_pipeline import _is_self_state_query

        for text in (
            "give me a quick status report",
            "how are you doing?",
            "are you ok?",
            "do you have any queued jobs right now?",
            "whats your queue looking like",
        ):
            self.assertTrue(_is_self_state_query(text), text)

    def test_life_content_is_not_state(self):
        from lisan.tools.memory_pipeline import _is_self_state_query

        for text in (
            "Maya asked how her project status looked to the teacher",
            "I waited in the queue at the DMV for two hours",
            "Frank fixed the fence today",
            "my job interview went well",
        ):
            self.assertFalse(_is_self_state_query(text), text)


if __name__ == "__main__":
    unittest.main()
