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


if __name__ == "__main__":
    unittest.main()
