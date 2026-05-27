"""Tests for the primer-derived known-cast index."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.tools.primer_index import known_names, name_in_text


class PrimerIndexTests(unittest.TestCase):
    def test_missing_primer_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(known_names(Path(tmp)), frozenset())

    def test_extracts_full_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "identity.md").write_text(
                "# Identity\n\nNadia Okonkwo, software engineer.\n"
                "Brother: Emeka Okonkwo. Friend: Sarah Cho.\n",
                encoding="utf-8",
            )
            names = known_names(vault)
            self.assertIn("Nadia Okonkwo", names)
            self.assertIn("Emeka Okonkwo", names)
            self.assertIn("Sarah Cho", names)
            # Individual tokens too, so first-name mentions match.
            self.assertIn("Nadia", names)
            self.assertIn("Sarah", names)

    def test_extracts_users_name_when_it_is_a_month(self) -> None:
        """User August Morgan must surface from the primer."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "identity.md").write_text(
                "# Identity\n\nAugust Morgan, lead developer.\n",
                encoding="utf-8",
            )
            names = known_names(vault)
            self.assertIn("August", names)
            self.assertIn("August Morgan", names)

    def test_handles_hyphenated_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "identity.md").write_text(
                "# Identity\n\nMaria Garcia-Lopez, mentor.\n",
                encoding="utf-8",
            )
            names = known_names(vault)
            self.assertIn("Maria", names)
            self.assertIn("Garcia", names)
            self.assertIn("Lopez", names)

    def test_cache_invalidates_on_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            primer = vault / "primer" / "identity.md"
            primer.write_text("# Identity\n\nMaya Smith.\n", encoding="utf-8")
            first = known_names(vault)
            self.assertIn("Maya", first)

            # Touch with new content + bump mtime.
            import os, time
            time.sleep(0.05)
            primer.write_text("# Identity\n\nBob Jones.\n", encoding="utf-8")
            os.utime(primer, None)
            second = known_names(vault)
            self.assertIn("Bob", second)
            self.assertNotIn("Maya", second)


class NameInTextTests(unittest.TestCase):
    def test_word_boundary_match(self) -> None:
        self.assertTrue(name_in_text("Marcus", "I spoke with Marcus today."))
        self.assertFalse(name_in_text("Marc", "I spoke with Marcus today."))

    def test_empty_inputs(self) -> None:
        self.assertFalse(name_in_text("", "hello"))
        self.assertFalse(name_in_text("Bob", ""))


if __name__ == "__main__":
    unittest.main()
