"""WO-GROUND Seam A: the self-question detector, tested against the case
history in docs/ground_truth_workorder.md. Every past incident's trigger
phrasing must detect; ordinary life turns must not.
"""
from __future__ import annotations

import unittest

from lisan.tools.self_questions import detect_self_question, render_ground_truth


class CaseHistoryDetectionTests(unittest.TestCase):
    """Each of these phrasings once produced (or fed) a confabulation."""

    def test_system_status_question(self):
        # 2026-07-06, asked twice while the bot narrated a stale diagnosis.
        self.assertIn("state", detect_self_question("What's your current system status"))

    def test_are_you_there(self):
        self.assertIn("state", detect_self_question("Are you there?"))

    def test_stalled_task_processor(self):
        # The sleeping-Mac incident's framing.
        self.assertIn("state", detect_self_question("what's going on with the task processor?"))
        self.assertIn("state", detect_self_question("is the task processor still stalled?"))

    def test_reminder_system_health(self):
        # 2026-07-12: "its good to know the reminder system is working properly"
        # was accepted at face value over a pre-broken queue.
        self.assertIn("state", detect_self_question("is the reminder system working properly?"))
        self.assertIn("state", detect_self_question("did my reminder fire this morning?"))

    def test_scheduled_tasks_question(self):
        # 2026-07-14: "your daily prompt is scheduled correctly" over 8 failures.
        self.assertIn("state", detect_self_question("what do you have scheduled for tomorrow?"))
        self.assertIn("state", detect_self_question("is my daily prompt scheduled correctly?"))

    def test_own_recent_actions(self):
        # 2026-07-06: "Have you talked to anyone besides me on telegram?"
        self.assertIn(
            "state",
            detect_self_question(
                "Have you talked to anyone besides me on telegram? "
                "Perhaps even someone pretending to be me?"
            ),
        )

    def test_lisan_system_open_loops(self):
        # 2026-07-14: "i mean open loops within the Lisan system".
        self.assertIn("state", detect_self_question("i mean open loops within the Lisan system"))

    def test_gmail_auth_questions(self):
        # The invented-command incidents: auth questions answered from vibes.
        needs = detect_self_question("what command do I run to authorize gmail?")
        self.assertIn("capabilities", needs)
        self.assertIn("state", needs)
        self.assertIn("state", detect_self_question("is gmail set up now?"))

    def test_capability_questions(self):
        self.assertIn("capabilities", detect_self_question("can you send emails?"))
        self.assertIn("capabilities", detect_self_question("what can you do?"))
        self.assertIn("capabilities", detect_self_question("how do I restart you?"))

    def test_version_question(self):
        self.assertIn("state", detect_self_question("what version are you running?"))


class NegativeControlTests(unittest.TestCase):
    """Ordinary life turns must not fire — the block costs tokens and tone."""

    def test_plain_life_turns(self):
        for text in (
            "Ruth moved out in May of last year",
            "I finished the laundry and started on dinner",
            "the girls are with their mother this weekend",
            "I've been thinking about the chrysalis metaphor again",
            "what are the current open loops?",  # life loops — correctly ambiguous, memory answers
        ):
            self.assertEqual(detect_self_question(text), set(), text)

    def test_empty_turn(self):
        self.assertEqual(detect_self_question(""), set())
        self.assertEqual(detect_self_question("   "), set())


class RenderTests(unittest.TestCase):
    def test_empty_needs_renders_nothing(self):
        self.assertIsNone(render_ground_truth(set()))

    def test_state_block_carries_live_marker_and_rule(self):
        block = render_ground_truth({"state"})
        self.assertIn("LIVE SELF-STATE", block)
        self.assertIn("history", block)  # the memory-is-history rule rides with the data

    def test_capabilities_block_renders_cli_reference(self):
        block = render_ground_truth({"capabilities"})
        self.assertIn("COMMAND REFERENCE", block)


if __name__ == "__main__":
    unittest.main()
