"""Tests for the operating-style preference parser (Finding #11)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import dump_markdown
from lisan.tools.operating_style import (
    emotion_naming_allowed,
    load_operating_style,
    prefers_directness,
)


class OperatingStyleFrontmatterTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            style = load_operating_style(vault)
            self.assertIsNone(style["emotion-naming"])
            self.assertIsNone(style["directness"])

    def test_json_frontmatter_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            fm = {
                "emotion-naming": False,
                "directness": True,
                "opener-style": "minimal",
                "summary-length": "short",
            }
            (vault / "primer" / "operating-style.md").write_text(
                dump_markdown(fm, "# Operating Style\n"),
                encoding="utf-8",
            )
            style = load_operating_style(vault)
            self.assertEqual(style["emotion-naming"], False)
            self.assertEqual(style["directness"], True)
            self.assertEqual(style["opener-style"], "minimal")
            self.assertEqual(style["summary-length"], "short")

    def test_emotion_naming_allowed_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            fm = {"emotion-naming": False}
            (vault / "primer" / "operating-style.md").write_text(
                dump_markdown(fm, "# Operating Style\n"),
                encoding="utf-8",
            )
            self.assertFalse(emotion_naming_allowed(vault))


class OperatingStyleLegacyTextTests(unittest.TestCase):
    """Legacy free-text primers without frontmatter."""

    def test_emotion_naming_phrase_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "operating-style.md").write_text(
                "# Operating Style\n\nNadia doesn't want emotions named prematurely.\n",
                encoding="utf-8",
            )
            self.assertFalse(emotion_naming_allowed(vault))

    def test_directness_phrase_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "operating-style.md").write_text(
                "# Operating Style\n\nUser values directness in all things.\n",
                encoding="utf-8",
            )
            self.assertTrue(prefers_directness(vault))


if __name__ == "__main__":
    unittest.main()
