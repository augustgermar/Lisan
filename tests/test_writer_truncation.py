"""Regression tests for Finding #7 (fallback writer truncation)."""

from __future__ import annotations

import unittest

from lisan.agents.writer import _truncate_summary, WriterAgent


class TruncateSummaryTests(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(_truncate_summary("hello world", 120), "hello world")

    def test_no_mid_word_truncation(self) -> None:
        text = "I called him this afternoon. He was more open than I expected — said the back has been a seven out of ten some mornings but he did not mention it"
        result = _truncate_summary(text, 120)
        # The bug produced "...mornings bu". The fix must NOT end mid-word.
        self.assertFalse(result.endswith(" bu"))
        # Should end with either an ellipsis (word boundary) or a period
        # (sentence boundary).
        self.assertTrue(result.endswith("…") or result.endswith("."))
        # Length stays within cap.
        self.assertLessEqual(len(result), 120 + 1)  # +1 for the ellipsis char

    def test_sentence_boundary_preferred_when_meaningful(self) -> None:
        # First sentence is meaningful (28 chars of a 120-char cap → above
        # 40% threshold), so cut at the period.
        text = "First sentence is decent length here. Second sentence is also reasonably long and detailed."
        result = _truncate_summary(text, 60)
        self.assertEqual(result, "First sentence is decent length here.")

    def test_falls_back_to_word_boundary_when_no_sentence(self) -> None:
        text = "no punctuation at all in this line just one long sentence of nothing in particular"
        result = _truncate_summary(text, 30)
        # Should end at a word boundary with ellipsis.
        self.assertFalse(result.endswith(" "))
        self.assertTrue(result.endswith("…"))
        # Last meaningful char before ellipsis is a letter, not a partial word.
        self.assertTrue(result[-2].isalpha())


class WriterSummaryWorkingCapTests(unittest.TestCase):
    def test_working_summary_cap_is_240(self) -> None:
        agent = WriterAgent.__new__(WriterAgent)  # bypass full __init__
        self.assertEqual(agent._WORKING_SUMMARY_CAP, 240)


if __name__ == "__main__":
    unittest.main()
