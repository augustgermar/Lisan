"""Embedding is slow; it must not run inside a write transaction.

2026-10-03: during `index.rebuild_all` the scheduler lanes, call logging and
capture all failed with "database is locked" for minutes. The embedding phase
issued `UPDATE files SET embedding_status` as each batch finished, which opened
a write transaction at the first batch and held SQLite's single writer lock
until the last one.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import lisan.tools.rebuild_index as ri
from lisan.tools.rebuild_index import open_index_connection


class _ProbingProvider:
    """Stands in for the embedder; each 'embedding' checks the writer lock is free."""

    settings = {"batch_size": 1}
    probes: list[str] = []

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def embed_records(self, contents):
        other = sqlite3.connect(self.db_path, timeout=0.2)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            self.probes.append("free")
        except sqlite3.OperationalError:
            self.probes.append("locked")
        finally:
            other.close()
        return SimpleNamespace(
            vectors=[[0.1, 0.2] for _ in contents], mode_used="semantic", model="m", dimension=2,
        )


class EmbedPhaseLockTests(unittest.TestCase):
    def _seed(self, conn: sqlite3.Connection, n: int) -> list[tuple[str, str]]:
        conn.executemany(
            "INSERT INTO files (id, type, path, created, updated, status) "
            "VALUES (?, 'entity', ?, '2026-10-03', '2026-10-03', 'active')",
            [(f"entity.e{i}", f"entities/e{i}.md") for i in range(n)],
        )
        conn.commit()
        return [(f"entity.e{i}", f"content {i}") for i in range(n)]

    def test_full_rebuild_embedding_does_not_hold_the_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lisan.sqlite"
            conn = open_index_connection(db_path)
            try:
                targets = self._seed(conn, 4)
                provider = _ProbingProvider(db_path)
                provider.probes = []
                with patch.object(ri, "EmbeddingProvider", lambda cfg: provider), patch.object(ri, "load_config", lambda: {}):
                    ri._embed_and_write(conn, targets, Path(tmp) / "embeddings.bin")
                self.assertEqual(provider.probes, ["free"] * 4, "embedding ran inside a write transaction")
                self.assertFalse(conn.in_transaction)
                statuses = {r[0] for r in conn.execute("SELECT embedding_status FROM files")}
                self.assertEqual(statuses, {"embedded"})
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
