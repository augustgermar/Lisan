"""Learned edges: co-selection mining is deterministic, NPMI discounts
ubiquitous records, authored links are excluded, and the retrieval lane
surfaces a learned partner the query never names."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from lisan.config import load_config
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.learned_edges import (
    ensure_learned_edges_table,
    learned_partners,
    mine_learned_edges,
)
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.record_factory import new_knowledge
from lisan.tools.retrieval import retrieve_context


def _log_event(conn: sqlite3.Connection, loaded: list[str]) -> None:
    conn.execute(
        "INSERT INTO retrieval_log (user_query, files_loaded) VALUES (?, ?)",
        ("q", json.dumps(loaded)),
    )


class MiningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "lisan.sqlite"
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(
            "CREATE TABLE retrieval_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_query TEXT, files_loaded TEXT);"
            "CREATE TABLE links (source_id TEXT, target_id TEXT, relationship_type TEXT);"
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _mine(self, **kwargs):
        self.conn.commit()
        return mine_learned_edges(self.db, **kwargs)

    def test_co_selection_produces_edges(self) -> None:
        for _ in range(5):
            _log_event(self.conn, ["a", "b"])
        for _ in range(5):
            _log_event(self.conn, ["c", "d"])
        stats = self._mine(min_co=3, min_npmi=0.3)
        self.assertEqual(stats["events"], 10)
        conn = sqlite3.connect(self.db)
        partners = learned_partners(conn, ["a"], limit=5)
        self.assertEqual([p[0] for p in partners], ["b"])
        self.assertGreater(partners[0][1], 0.9)  # perfect co-occurrence → npmi ≈ 1

    def test_ubiquitous_records_score_low(self) -> None:
        # "primer" appears in EVERY event; a/b co-occur only with each other.
        for i in range(6):
            _log_event(self.conn, ["primer", "a", "b"] if i < 3 else ["primer", f"x{i}", f"y{i}"])
        self._mine(min_co=2, min_npmi=0.5)
        conn = sqlite3.connect(self.db)
        partners = dict(learned_partners(conn, ["a"], limit=5))
        self.assertIn("b", partners)
        self.assertNotIn("primer", partners)  # NPMI discounts the everywhere-record

    def test_authored_links_are_excluded(self) -> None:
        self.conn.execute("INSERT INTO links VALUES ('a', 'b', 'related')")
        for _ in range(5):
            _log_event(self.conn, ["a", "b"])
        self._mine(min_co=2, min_npmi=0.1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(learned_partners(conn, ["a"], limit=5), [])

    def test_mining_is_deterministic_and_idempotent(self) -> None:
        for _ in range(4):
            _log_event(self.conn, ["a", "b", "c"])
        first = self._mine(min_co=2, min_npmi=0.1)
        second = self._mine(min_co=2, min_npmi=0.1)
        self.assertEqual(first, second)

    def test_partners_exclude_seeds_and_respect_limit(self) -> None:
        for _ in range(5):
            _log_event(self.conn, ["a", "b", "c", "d"])
        _log_event(self.conn, ["e", "f"])  # noise: keeps p(pair) < 1
        self._mine(min_co=2, min_npmi=0.05)
        conn = sqlite3.connect(self.db)
        partners = learned_partners(conn, ["a", "b"], limit=1, exclude={"a", "b"})
        self.assertEqual(len(partners), 1)
        self.assertNotIn(partners[0][0], {"a", "b"})

    def test_missing_table_returns_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertEqual(learned_partners(conn, ["a"], limit=3), [])


class RetrievalLaneTests(unittest.TestCase):
    def test_learned_partner_enters_fusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_repo_layout(root)
            vault = vault_root(root)
            db_path = root / "lisan.sqlite"
            new_knowledge(vault, "Orchard ladder safety",
                          summary="Orchard ladder safety notes for the tall pear tree.",
                          body="Orchard ladder safety notes for pruning the tall pear tree.")
            new_knowledge(vault, "Cider press maintenance",
                          summary="Cider press maintenance and gasket replacement.",
                          body="The cider press needs its gasket replaced before autumn.")
            rebuild_index(vault=vault, db_path=db_path, embeddings_file=root / "embeddings.bin")

            conn = sqlite3.connect(db_path)
            ladder = conn.execute("SELECT id FROM files WHERE summary LIKE '%ladder%'").fetchone()[0]
            press = conn.execute("SELECT id FROM files WHERE summary LIKE '%press%'").fetchone()[0]
            ensure_learned_edges_table(conn)
            for _ in range(5):
                _log_event(conn, [ladder, press])
            _log_event(conn, ["unrelated.one", "unrelated.two"])  # keeps p(pair) < 1
            conn.commit()
            conn.close()
            mine_learned_edges(db_path, min_co=3, min_npmi=0.3)

            config = deepcopy(load_config())
            config.setdefault("retrieval", {})["learned_edges"] = {"enabled": True, "seed_count": 3, "lane_limit": 5}
            with patch("lisan.tools.retrieval.load_config", return_value=config):
                result = retrieve_context("orchard ladder safety", domain="environmental",
                                          vault=vault, db_path=db_path)
            press_item = next((i for i in result.loaded if i.id == press), None)
            self.assertIsNotNone(press_item, f"learned partner missing; loaded={[i.id for i in result.loaded]}")
            self.assertIn("learned_edge", press_item.reason)


if __name__ == "__main__":
    unittest.main()
