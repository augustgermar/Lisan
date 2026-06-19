from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.cli import main
from lisan.config import save_default_config


class CliBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "fresh-vault"
        self.db_path = self.root / "lisan.sqlite"
        self.embeddings_path = self.root / "embeddings.bin"
        save_default_config(self.root / "config.yaml")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patch_runtime(self) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(patch("lisan.cli.repo_root", return_value=self.root))
        stack.enter_context(patch("lisan.cli.sqlite_path", return_value=self.db_path))
        stack.enter_context(patch("lisan.tools.health_report.sqlite_path", return_value=self.db_path))
        stack.enter_context(patch("lisan.tools.rebuild_index.sqlite_path", return_value=self.db_path))
        stack.enter_context(patch("lisan.tools.rebuild_index.embeddings_path", return_value=self.embeddings_path))
        stack.enter_context(patch("lisan.tools.health_report._embeddings_health_lines", return_value=["- embeddings skipped in test"]))
        return stack

    def test_health_bootstraps_fresh_vault_and_schema(self) -> None:
        stdout = io.StringIO()
        with self._patch_runtime(), contextlib.redirect_stdout(stdout):
            code = main(["health", "--vault", str(self.vault)])

        self.assertEqual(code, 0)
        self.assertTrue((self.vault / "primer" / "identity.md").exists())
        self.assertTrue((self.vault / "reports" / "health-latest.md").exists())
        self.assertTrue(self.db_path.exists())

        conn = sqlite3.connect(self.db_path)
        try:
            files_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(files_table)

    def test_sync_bootstraps_missing_current_brief_and_schema(self) -> None:
        stdout = io.StringIO()
        with self._patch_runtime(), contextlib.redirect_stdout(stdout):
            code = main(["sync", "--vault", str(self.vault)])

        self.assertEqual(code, 0)
        self.assertTrue((self.vault / "primer" / "current-brief.md").exists())
        self.assertTrue((self.vault / "reports" / "health-latest.md").exists())
        self.assertTrue(self.db_path.exists())

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute("SELECT COUNT(*) AS count FROM files").fetchone()["count"]
        finally:
            conn.close()
        self.assertGreaterEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
