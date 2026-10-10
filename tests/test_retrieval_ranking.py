from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from lisan.frontmatter import dump_markdown
from lisan.tools.retrieval_layers import (
    _LayerCandidate, _fuse_ranked_candidates, _rank_quality_multiplier,
    _recent_access_counts,
)
from lisan.tools.retrieval_graph import _load_relevant_contradictions


def _row(**updates):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE f (id TEXT, type TEXT, status TEXT, significance TEXT, updated TEXT, created TEXT, summary TEXT, path TEXT)"
    )
    values = {
        "id": "r", "type": "knowledge", "status": "active", "significance": "low",
        "updated": "2026-01-01", "created": "2026-01-01",
        "summary": "test claim", "path": "claims/test.md",
    }
    values.update(updates)
    conn.execute("INSERT INTO f VALUES (?,?,?,?,?,?,?,?)", tuple(values.values()))
    return conn.execute("SELECT * FROM f").fetchone()


class RetrievalRankingSignalsTests(unittest.TestCase):
    def test_significance_affects_rank_quality(self):
        low = _rank_quality_multiplier(_row(significance="low"), query="", today=date(2026, 1, 1))
        high = _rank_quality_multiplier(_row(significance="high"), query="", today=date(2026, 1, 1))
        self.assertGreater(high, low)

    def test_old_and_frequently_recalled_records_are_gently_demoted(self):
        fresh = _rank_quality_multiplier(_row(), query="", today=date(2026, 1, 1))
        old = _rank_quality_multiplier(_row(updated="2020-01-01"), query="", today=date(2026, 1, 1))
        frequent = _rank_quality_multiplier(_row(), query="", today=date(2026, 1, 1), access_count=50)
        self.assertGreater(fresh, old)
        self.assertGreater(fresh, frequent)
        self.assertGreater(old, 0)
        self.assertGreater(frequent, 0)

    def test_typed_intent_distinguishes_decisions_from_temporal_events(self):
        today = date(2026, 1, 1)
        decision = _row(type="decision")
        episode = _row(type="episode")
        self.assertGreater(
            _rank_quality_multiplier(decision, query="what decision did we make", today=today),
            _rank_quality_multiplier(episode, query="what decision did we make", today=today),
        )
        self.assertGreater(
            _rank_quality_multiplier(episode, query="what happened historically", today=today),
            _rank_quality_multiplier(decision, query="what happened historically", today=today),
        )

    def test_contradicted_memory_is_demoted_for_current_queries_but_not_history(self):
        row = _row(status="disputed")
        current = _rank_quality_multiplier(row, query="current status", today=date(2026, 1, 1))
        historical = _rank_quality_multiplier(row, query="what was true historically", today=date(2026, 1, 1))
        self.assertGreater(historical, current)

    def test_recent_access_counts_come_from_derived_retrieval_log(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE retrieval_log (id INTEGER, files_loaded TEXT)")
        conn.executemany("INSERT INTO retrieval_log VALUES (?,?)", [
            (1, '["a", "b"]'), (2, '["a"]'), (3, 'not-json'),
        ])
        self.assertEqual(_recent_access_counts(conn), {"a": 2, "b": 1})

    def test_new_vendor_claim_wins_and_conflict_is_surfaced(self):
        old = _row(id="vendor-a", type="claim", status="disputed", updated="2026-01-01")
        new = _row(id="vendor-b", type="claim", status="active", updated="2026-02-01")
        ranked, _ = _fuse_ranked_candidates(
            rows_by_id={"vendor-a": old, "vendor-b": new},
            sql_candidates=[],
            fts_candidates=[
                _LayerCandidate("vendor-a", 2.0, "fts_bm25"),
                _LayerCandidate("vendor-b", 1.0, "fts_bm25"),
            ],
            vector_candidates=[], rrf_k=60, fused_limit=2,
            query="current vendor", today=date(2026, 5, 1),
        )
        self.assertEqual(ranked[0].id, "vendor-b")

        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            (vault / "contradictions").mkdir()
            path = vault / "contradictions" / "vendor-switch.md"
            path.write_text(dump_markdown({
                "id": "contradiction.vendor-switch", "type": "contradiction_log",
                "status": "active", "created": "2026-02-01",
                "summary": "Vendor A was replaced by Vendor B in February.",
            }, "Earlier memory says Vendor A; later evidence says Vendor B."), encoding="utf-8")
            notes = _load_relevant_contradictions(vault, "current vendor")
        self.assertTrue(notes)
        self.assertIn("Vendor A", notes[0])
        self.assertIn("Vendor B", notes[0])


if __name__ == "__main__":
    unittest.main()
