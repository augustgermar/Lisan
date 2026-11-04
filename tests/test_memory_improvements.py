from __future__ import annotations

import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from lisan.tools.context_budget import bound_final_context
from lisan.tools.entity_story import shared_index_text
from lisan.tools.freshness import metadata
from lisan.tools.judge import validate_score_ids
from lisan.tools.retrieval_layers import _fts_ranked_candidates


class MemoryImprovementTests(unittest.TestCase):
    def test_fts_paginates_past_unbounded_excluded_hits(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE VIRTUAL TABLE files_fts USING fts5(id, summary, content)")
        conn.execute(
            "CREATE TABLE files (id TEXT, type TEXT, status TEXT, path TEXT, batch_id TEXT, artifact_ref TEXT, "
            "ingestion_status TEXT, summary TEXT)"
        )
        rows = []
        for i in range(300):
            record_id = f"excluded-{i:03d}"
            rows.append((record_id, "knowledge", "active", f"knowledge/{i}.md", "bad-batch", "", "", "needle"))
            conn.execute("INSERT INTO files_fts VALUES (?, ?, ?)", (record_id, "needle", "needle"))
        rows.append(("eligible", "knowledge", "active", "knowledge/eligible.md", "", "", "", "needle"))
        conn.execute("INSERT INTO files_fts VALUES (?, ?, ?)", ("eligible", "needle", "needle"))
        conn.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        file_rows = conn.execute("SELECT * FROM files").fetchall()
        candidates, mode = _fts_ranked_candidates(
            conn,
            file_rows=file_rows,
            query="needle",
            active_contexts={"all"},
            quarantined_artifact_ids=set(),
            quarantined_batch_ids={"bad-batch"},
            include_quarantined=False,
            limit=1,
        )
        self.assertEqual(mode, "bm25")
        self.assertEqual([candidate.id for candidate in candidates], ["eligible"])

    def test_final_context_budget_is_hard_and_reports_omission(self) -> None:
        bounded, accounting = bound_final_context("a" * 500, budget_chars=120)
        self.assertLessEqual(len(bounded), 120)
        self.assertEqual(accounting["retrieved_chars"], 500)
        self.assertTrue(accounting["omitted"])
        for budget in (0, -1, 1, 10):
            tiny, _ = bound_final_context("x" * 500, budget_chars=budget)
            self.assertLessEqual(len(tiny), max(0, budget))

    def test_shared_index_text_keeps_source_log_after_compaction(self) -> None:
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "entity.log"
            archive.write_text('{"text": "Ruth documented cobalt migration.", "date": "2025-01-02"}\n')
            text = shared_index_text(
                {"canonical_name": "Ruth", "source_log": [
                    {"text": "Ruth prefers the cedar room.", "date": "2025-01-03"}
                ]},
                "# Ruth\n\nRuth lives near the coast.",
                archive_path=archive,
            )
        self.assertIn("cobalt migration", text)
        self.assertIn("cedar room", text)
        self.assertIn("lives near the coast", text)

    def test_missing_observation_date_is_not_now_and_review_due_is_not_expiry(self) -> None:
        fresh = metadata({"summary": "undated fact", "review_after": "2000-01-01"})
        self.assertEqual(fresh.observed_at, "unknown")
        self.assertEqual(fresh.status, "unknown")
        dated = metadata({"observed_at": "2020-01-01", "review_after": "2000-01-01"})
        self.assertEqual(dated.status, "current")
        self.assertIsNone(dated.valid_until)

    def test_historical_selection_honors_validity_interval(self) -> None:
        from lisan.tools.retrieval_layers import _row_valid_at
        row = {"valid_from": "2026-01-10", "valid_to": "2026-02-01"}
        class Row(dict):
            def keys(self): return super().keys()
        wrapped = Row(row)
        from datetime import date
        self.assertTrue(_row_valid_at(wrapped, date(2026, 1, 31)))
        self.assertFalse(_row_valid_at(wrapped, date(2026, 2, 1)))
        self.assertFalse(_row_valid_at(wrapped, date(2026, 1, 9)))

    def test_judge_scores_require_exact_rubric_ids(self) -> None:
        rubric = {"dimensions": [{"id": "continuity"}, {"id": "grounding"}]}
        self.assertEqual(
            validate_score_ids([{"id": "continuity", "score": 5}], rubric)[0],
            False,
        )
        self.assertEqual(
            validate_score_ids(
                [{"id": "continuity", "score": 5}, {"id": "grounding", "score": None}],
                rubric,
            )[0],
            True,
        )
        for bad in (True, 1.5, 0, 6, "5"):
            ok, _ = validate_score_ids(
                [{"id": "continuity", "score": bad}, {"id": "grounding", "score": None}], rubric
            )
            self.assertFalse(ok)

    def test_source_excerpt_coordinates_are_explicit(self) -> None:
        from lisan.frontmatter import dump_markdown
        from lisan.tools.retrieval_graph import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.md"
            path.write_text(dump_markdown({"type": "evidence", "summary": "fact"}, "line one\nline two"))
            rendered = _format_item_detail(
                RetrievalItem("e", "evidence", "evidence.md", "fact", 1.0, "test"), path
            )
        self.assertIn("derived canonical body lines 1-2", rendered)

    def test_source_provenance_is_original_or_explicitly_derived(self) -> None:
        from lisan.frontmatter import dump_markdown
        from lisan.tools.retrieval_graph import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.md"
            path.write_text(dump_markdown({"type": "evidence", "summary": "fact", "source_document": "mail.eml", "source_section": "body", "source_ref": "msg-7"}, "fact body"))
            rendered = _format_item_detail(RetrievalItem("e", "evidence", "evidence.md", "fact", 1.0, "test"), path, query="fact")
        self.assertIn("source_document=mail.eml", rendered)
        self.assertIn("not an original source coordinate", rendered)

    def test_entity_rendering_selects_query_relevant_archived_source(self) -> None:
        from lisan.frontmatter import dump_markdown
        from lisan.tools.entity_story import source_log_archive_path
        from lisan.tools.retrieval_graph import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem

        with TemporaryDirectory() as tmp:
            vault = Path(tmp)
            path = vault / "entities" / "people" / "ruth.md"
            path.parent.mkdir(parents=True)
            path.write_text(dump_markdown({
                "type": "entity", "canonical_name": "Ruth", "summary": "Ruth", "source_log": [],
            }, "Ruth has a quiet narrative."))
            archive = source_log_archive_path(vault, path)
            archive.parent.mkdir(parents=True)
            archive.write_text('{"text": "Ruth documented a cobalt migration project.", "date": "2025-01-02"}\n')
            rendered = _format_item_detail(
                RetrievalItem("entity.ruth", "entity", str(path.relative_to(vault)), "Ruth", 1.0, "test"),
                path,
                query="cobalt migration",
                vault=vault,
            )
        self.assertIn("cobalt migration", rendered)
        self.assertIn("source_passage", rendered)

    def test_knowledge_rendering_selects_matching_body_lines(self) -> None:
        from lisan.frontmatter import dump_markdown
        from lisan.tools.retrieval_graph import _format_item_detail
        from lisan.tools.retrieval_layers import RetrievalItem

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.md"
            path.write_text(dump_markdown({
                "type": "knowledge", "summary": "migration notes", "source_document": "fixture",
            }, "Unrelated opening.\n\nCobalt migration requires a staged cutover.\n\nUnrelated close."))
            rendered = _format_item_detail(
                RetrievalItem("knowledge.migration", "knowledge", "knowledge.md", "migration notes", 1.0, "test"),
                path,
                query="cobalt migration cutover",
            )
        self.assertIn("Cobalt migration requires a staged cutover", rendered)

    def test_passage_vectors_keep_record_identity_and_support_legacy_vectors(self) -> None:
        from lisan.tools.rebuild_index import _passage_targets
        from lisan.tools.vector_store import EmbeddingIndex, VectorScorer

        targets = _passage_targets("record.r", "alpha paragraph\n\n" + "beta " * 700)
        self.assertGreater(len(targets), 1)
        self.assertTrue(all(item_id.startswith("record.r::passage:") for item_id, _ in targets))
        scorer = VectorScorer([1.0, 0.0], EmbeddingIndex("test", 2, {
            targets[0][0]: [0.0, 1.0], targets[-1][0]: [1.0, 0.0],
        }), "semantic")
        self.assertAlmostEqual(scorer.score("record.r"), 1.0)
        legacy = VectorScorer([1.0, 0.0], EmbeddingIndex("legacy", 2, {"record.r": [1.0, 0.0]}), "semantic")
        self.assertAlmostEqual(legacy.score("record.r"), 1.0)


if __name__ == "__main__":
    unittest.main()
