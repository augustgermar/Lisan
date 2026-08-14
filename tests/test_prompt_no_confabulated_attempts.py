"""The honesty rule must cover attempts and reasons, not just successes.

On 2026-08-14 Jake told the owner: "I tried to log this as a check-in, but the
check-in tool isn't available on this channel, so it was not recorded." Every
clause was false. `checkin` was in AVAILABLE_TOOLS, it resolved the owner three
different ways, 31 check-in records already existed, and the service log shows
**zero** checkin attempts since the restart — the call was never made.

He borrowed "on this channel" from the rule directly above, which is about
approvals, and applied it to a tool he simply had not invoked.

The rule as written forbade claiming you *performed* an action. It said nothing
about claiming you *tried*, so a fabricated attempt with a fabricated cause
went straight through. That failure is worse than a false success claim: it
sent the owner to debug a tool that was working perfectly, and it would have
kept doing so.

These tests pin the extension in BOTH prompts that carry the rule. The live
Telegram path is `conversation_v1.md` — `interlocutor_v1.md` is a different
agent's prompt, and a behavioural rule added only to the latter has already
cost this project a session (2026-07-26, commit 0feaf7a).
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / "prompts"

# Every prompt that carries the "never claim you performed an action" rule has
# to carry the attempt clause too, or the gap reopens in whichever one was
# missed.
CARRIERS = ("conversation_v1.md", "interlocutor_v1.md")


class AttemptHonestyTests(unittest.TestCase):
    def _text(self, name: str) -> str:
        """Whitespace-collapsed, because these prompts are hard-wrapped.

        The first version of this test asserted raw substrings and failed on
        both prompts — the phrases it looked for were split across a newline
        and an indent. A prompt test that breaks on rewrapping is a test that
        gets deleted the first time someone reflows a paragraph.
        """
        raw = (PROMPTS / name).read_text(encoding="utf-8")
        return " ".join(raw.split())

    def test_every_carrier_of_the_rule_is_known_to_this_test(self):
        """If a new prompt gains the rule, it must be added here deliberately."""
        found = {
            p.name for p in PROMPTS.glob("*.md")
            if "claim you performed an action" in " ".join(
                p.read_text(encoding="utf-8").split()).lower()
        }
        self.assertEqual(
            found, set(CARRIERS),
            f"prompts carrying the honesty rule changed: {sorted(found)}. "
            "Extend CARRIERS and make sure the new prompt covers attempts too.",
        )

    def test_the_rule_covers_attempts_not_only_successes(self):
        for name in CARRIERS:
            text = self._text(name)
            lowered = text.lower()
            with self.subTest(prompt=name):
                self.assertIn("never say you tried", lowered,
                              f"{name}: nothing forbids claiming an attempt that never happened")
                self.assertIn("tool_result", lowered,
                              f"{name}: the attempt rule must anchor on TOOL_RESULT evidence")

    def test_the_rule_covers_invented_reasons(self):
        """The second half: not just "I tried" but "…and here is why it failed"."""
        for name in CARRIERS:
            lowered = self._text(name).lower()
            with self.subTest(prompt=name):
                self.assertIn("why something failed", lowered,
                              f"{name}: nothing forbids inventing a cause for a failure")

    def test_the_live_chat_prompt_names_the_exact_confabulation(self):
        """conversation_v1.md is the Telegram path. It should carry the concrete
        sentence that was actually produced, not only the abstract rule —
        specific negative examples are what these prompts respond to."""
        lowered = self._text("conversation_v1.md").lower()
        self.assertIn("isn't available", lowered)
        self.assertIn("available_tools", lowered)

    def test_it_says_what_to_do_instead(self):
        """A prohibition with no replacement gets paraphrased into a new
        excuse. The prompt has to supply the honest sentence."""
        lowered = self._text("conversation_v1.md").lower()
        self.assertIn("didn't log that", lowered)


if __name__ == "__main__":
    unittest.main()
