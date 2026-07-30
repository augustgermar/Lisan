"""A bare `except Exception` turned a missing import into a permanent no-op.

`epistemic.load_existing_patterns` called `load_markdown` without importing it.
The NameError was caught by `except Exception: continue`, so the function
returned `[]` for every pattern on disk, every time, since it was written —
and `analyst_ops` feeds that list to `pattern_conflicts_with_existing`, which
therefore never once had data to compare a new hypothesis against. Measured on
a production vault 2026-07-29: 7 pattern files, 0 records returned.

The lesson is the exception width, not the import. A crash would have been
found in a day; a swallowed crash looked like "no existing patterns" forever,
and the analyst went on re-deriving findings it already held.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lisan.frontmatter import write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.epistemic import load_existing_patterns


def _pattern(record_id: str, hypothesis: str) -> dict:
    return {
        "id": record_id, "type": "pattern", "created": "2026-07-29",
        "updated": "2026-07-29", "status": "candidate", "significance": "medium",
        "domain_primary": "cross_arena", "domain_secondary": [], "privacy": "personal",
        "summary": hypothesis, "links": [], "confidence": 0.4,
        "confidence_basis": "test", "last_confirmed": "2026-07-29",
        "review_after": "2026-07-29", "pattern_type": "work_loop",
        "hypothesis": hypothesis, "supporting_records": [], "counterexamples": [],
        "alternative_explanations": [], "first_seen": "2026-07-29",
        "last_reviewed": "2026-07-29", "predictions": [], "review_notes": "",
    }


class LoadExistingPatternsTests(unittest.TestCase):
    def test_patterns_on_disk_are_actually_returned(self):
        with TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            ensure_vault_layout(vault)
            for n in range(3):
                write_markdown(
                    vault / "patterns" / f"2026-07-2{n}-p{n}.md",
                    _pattern(f"pattern.p{n}", f"hypothesis number {n}"),
                    "# p\n",
                )
            loaded = load_existing_patterns(vault)
        self.assertEqual(len(loaded), 3, "a pattern on disk must reach the conflict check")
        self.assertTrue(all("hypothesis" in p for p in loaded))

    def test_no_patterns_directory_is_empty_not_an_error(self):
        with TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            self.assertEqual(load_existing_patterns(vault), [])

    def test_a_malformed_file_does_not_take_the_loader_down_with_it(self):
        """The narrow except still tolerates a bad file — that was its legitimate
        job — while a NameError or AttributeError now surfaces instead of being
        silently absorbed.

        Note what a frontmatter-less file actually produces: `load_markdown`
        does not raise, so normalization yields a record with no `id` rather
        than nothing at all. That is worth knowing and is deliberately asserted
        rather than smoothed over — a loader that quietly emits id-less records
        is a smaller version of the same problem this test exists for."""
        with TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            ensure_vault_layout(vault)
            (vault / "patterns" / "broken.md").write_text("not frontmatter at all\n", encoding="utf-8")
            write_markdown(vault / "patterns" / "good.md", _pattern("pattern.good", "a real one"), "# g\n")
            loaded = load_existing_patterns(vault)
        ids = [record.get("id") for record in loaded]
        self.assertIn("pattern.good", ids, "the real pattern must reach the conflict check")
        self.assertIn(None, ids, "and the frontmatter-less file surfaces as an id-less record")


if __name__ == "__main__":
    unittest.main()
