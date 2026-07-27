from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lisan.paths as paths


class DataRootContainmentTests(unittest.TestCase):
    """Mutable data paths must never resolve into the live install during a
    test run. On 2026-07-27 a full-suite run silently replaced a populated
    production embeddings.bin (971 vectors) with an empty stub, because
    sqlite_path() and embeddings_path() both defaulted to repo_root(). The
    same ambient resolution had already been logged as a deviation candidate
    for sqlite_path."""

    def test_data_root_honours_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LISAN_DATA_HOME": tmp}):
                self.assertEqual(paths.data_root(), Path(tmp))
                self.assertEqual(paths.sqlite_path(), Path(tmp) / "lisan.sqlite")
                self.assertEqual(paths.embeddings_path(), Path(tmp) / "embeddings.bin")

    def test_data_root_defaults_to_the_install(self):
        env = {k: v for k, v in os.environ.items() if k != "LISAN_DATA_HOME"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.data_root(), paths.repo_root())

    def test_resource_paths_are_not_redirected(self):
        """Read-only package resources must still come from the real package,
        or redirecting the data root would break prompt/schema loading."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LISAN_DATA_HOME": tmp}):
                self.assertEqual(paths.repo_root(), Path(paths.__file__).resolve().parents[1])
                self.assertNotIn(tmp, str(paths.schemas_dir()))
                self.assertTrue(str(paths.schemas_dir()).endswith("schemas"))


class EmptyIndexOverwriteTests(unittest.TestCase):
    """The runner-independent guard. LISAN_DATA_HOME (set in tests/__init__.py)
    protects pytest, but `unittest discover` does not import that package init
    — the same gap that let the outbound kill switch fail on 2026-07-26. So the
    real protection lives at the write itself, where it holds no matter who
    calls it: a test, a stray script, or a future agent rebuilding with a
    broken embedder."""

    def test_empty_index_refuses_to_replace_a_populated_one(self):
        from lisan.tools.vector_store import EmptyIndexOverwrite, write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", [0.1, 0.2]), ("b", [0.3, 0.4])], model="m", dimension=2)
            populated = path.read_text()

            with self.assertRaises(EmptyIndexOverwrite):
                write_embeddings(path, [("a", None), ("b", None)], model="none", dimension=0)

            self.assertEqual(path.read_text(), populated)  # untouched

    def test_empty_index_is_fine_when_there_is_nothing_to_lose(self):
        from lisan.tools.vector_store import write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", None)], model="none", dimension=0)  # fresh install
            self.assertTrue(path.exists())

    def test_populated_index_may_be_replaced_by_another_populated_one(self):
        from lisan.tools.vector_store import write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", [0.1])], model="m", dimension=1)
            write_embeddings(path, [("a", [0.9]), ("b", [0.8])], model="m", dimension=1)
            self.assertIn("0.9", path.read_text())


if __name__ == "__main__":
    unittest.main()
