"""Regression test for Finding #6 (duplicate drafts from retries)."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from lisan.tools.memory_pipeline import _write_draft


def _fake_listener() -> dict:
    return {"memory_type": "episode", "memory_score": 7}


def _fake_writer(summary: str) -> dict:
    return {
        "summary": summary,
        "significance": "medium",
        "frontmatter": {"confidence": "medium", "confidence_basis": "test"},
        "sections": {"event_timeline": "an event occurred"},
    }


def _fake_skeptic(approved: bool = True) -> dict:
    return {"approved": approved, "recommended_action": "approve", "issues": []}


def _fake_interlocutor() -> dict:
    return {"response": "Heard.", "questions": []}


class DraftDedupTests(unittest.TestCase):
    def test_identical_text_produces_identical_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "drafts").mkdir()
            (vault / "transcripts").mkdir()
            transcript_path = vault / "transcripts" / "2026-05-27.md"
            transcript_path.write_text("# Transcript\n", encoding="utf-8")

            text = "Marcus pulled the feature again three days before sprint close."
            writer = _fake_writer("Marcus pulled the feature again three days before sprint close.")

            path_a = _write_draft(
                vault, text, transcript_path,
                _fake_listener(), writer, _fake_skeptic(), _fake_interlocutor(),
                task="episode", mode="capture", action="full", skeptic_approved=True,
            )
            path_b = _write_draft(
                vault, text, transcript_path,
                _fake_listener(), writer, _fake_skeptic(), _fake_interlocutor(),
                task="episode", mode="capture", action="full", skeptic_approved=True,
            )
            self.assertEqual(path_a, path_b,
                             "Identical source text must produce one draft file")
            # Only one file in the drafts directory.
            files = list((vault / "drafts").glob("*.md"))
            self.assertEqual(len(files), 1, f"Expected 1 draft, got {len(files)}")

    def test_filename_contains_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "drafts").mkdir()
            (vault / "transcripts").mkdir()
            transcript_path = vault / "transcripts" / "2026-05-27.md"
            transcript_path.write_text("# Transcript\n", encoding="utf-8")

            text = "Different text for this test entirely."
            expected_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            path = _write_draft(
                vault, text, transcript_path,
                _fake_listener(), _fake_writer("summary"),
                _fake_skeptic(), _fake_interlocutor(),
                task="episode", mode="capture", action="full", skeptic_approved=True,
            )
            self.assertIn(expected_hash, path.name)

    def test_different_text_produces_different_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "drafts").mkdir()
            (vault / "transcripts").mkdir()
            transcript_path = vault / "transcripts" / "2026-05-27.md"
            transcript_path.write_text("# Transcript\n", encoding="utf-8")

            path_a = _write_draft(
                vault, "First message text",
                transcript_path, _fake_listener(), _fake_writer("a"),
                _fake_skeptic(), _fake_interlocutor(),
                task="episode", mode="capture", action="full", skeptic_approved=True,
            )
            path_b = _write_draft(
                vault, "Second message text",
                transcript_path, _fake_listener(), _fake_writer("b"),
                _fake_skeptic(), _fake_interlocutor(),
                task="episode", mode="capture", action="full", skeptic_approved=True,
            )
            self.assertNotEqual(path_a, path_b)


if __name__ == "__main__":
    unittest.main()
