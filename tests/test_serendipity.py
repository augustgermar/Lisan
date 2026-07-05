"""Serendipity slots: a reserved tail slot samples the mid-tier, seeded from
the query so identical queries reproduce identical picks."""
from __future__ import annotations

import sqlite3
import unittest

from lisan.tools.retrieval_layers import _LayerCandidate, _fuse_ranked_candidates


def _rows(n: int) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE files (id TEXT, type TEXT, summary TEXT, path TEXT, created TEXT, updated TEXT, "
        "status TEXT, significance TEXT, domain_primary TEXT, privacy TEXT, confidence TEXT)"
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"rec-{i:02d}", "knowledge", f"summary {i}", f"knowledge/{i}.md",
             "2026-07-01", "2026-07-01", "active", "low", "cross_arena", "personal", "low"),
        )
    return {str(row["id"]): row for row in conn.execute("SELECT * FROM files")}


def _fuse(seed: str, slots: int, n: int = 40, limit: int = 10):
    rows_by_id = _rows(n)
    # One lane ranks everything in id order: rec-00 strongest.
    lane = [_LayerCandidate(id=f"rec-{i:02d}", score=float(n - i), source="fts_bm25") for i in range(n)]
    return _fuse_ranked_candidates(
        rows_by_id=rows_by_id,
        sql_candidates=[],
        fts_candidates=lane,
        vector_candidates=[],
        rrf_k=60,
        fused_limit=limit,
        serendipity_slots=slots,
        serendipity_seed=seed,
    )[0]


class SerendipityTests(unittest.TestCase):
    def test_slot_swaps_tail_for_mid_tier_pick(self) -> None:
        items = _fuse("query-a", slots=1)
        self.assertEqual(len(items), 10)
        pick = items[-1]
        self.assertEqual(pick.reason, "serendipity")
        # The pick comes from beyond the fused top (the remainder's mid band).
        self.assertNotIn(pick.id, [f"rec-{i:02d}" for i in range(10)])
        # The top of the ranking is untouched.
        self.assertEqual(items[0].id, "rec-00")

    def test_same_seed_reproduces_same_pick(self) -> None:
        a = _fuse("stable query", slots=1)
        b = _fuse("stable query", slots=1)
        self.assertEqual([i.id for i in a], [i.id for i in b])

    def test_different_seeds_can_differ(self) -> None:
        picks = {_fuse(f"query-{k}", slots=1)[-1].id for k in range(8)}
        self.assertGreater(len(picks), 1)  # not the same record every time

    def test_zero_slots_is_a_noop(self) -> None:
        items = _fuse("query", slots=0)
        self.assertTrue(all(item.reason.startswith("rrf") for item in items))

    def test_no_remainder_means_no_swap(self) -> None:
        items = _fuse("query", slots=1, n=8, limit=10)  # everything fits: no remainder
        self.assertTrue(all(item.reason.startswith("rrf") for item in items))


if __name__ == "__main__":
    unittest.main()
