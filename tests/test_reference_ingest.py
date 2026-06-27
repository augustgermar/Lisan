from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.document_chunker import chunk_document
from lisan.tools.ingest import _extract_pdf_text, ingest_reference_sources
from lisan.tools.record_factory import new_entity


class ReferenceIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        self.src = self.root / "source"
        self.src.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_chunk_document_merges_short_sections_and_splits_long_sections(self) -> None:
        text = """# SDP Manual

## Preface
Short intro.

## Budget Authority
""" + ("Budget details. " * 900) + """

## Participant Rights
Participants keep control over their budgets.
"""
        chunks = chunk_document(text, "SDP Manual", source_ref_base="sdp-manual.md", min_words=20, max_words=250)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(any("Preface" in chunk.body for chunk in chunks))
        self.assertTrue(any("Budget Authority" in chunk.title for chunk in chunks))
        self.assertTrue(all(chunk.total_chunks == len(chunks) for chunk in chunks))
        self.assertTrue(all(chunk.source_ref.startswith("sdp-manual.md") for chunk in chunks))

    def test_reference_ingest_creates_knowledge_and_links_entities(self) -> None:
        maya = new_entity(self.vault, "Maya", subtype="person", summary="Maya is the principal's daughter.")
        doc = self.src / "sdp-manual.md"
        doc.write_text(
            "# SDP Training Manual\n\n## Budget Authority\n\nMaya can reallocate SDP funds through the Regional Center.\n",
            encoding="utf-8",
        )

        result = ingest_reference_sources([doc], vault=self.vault, db_path=self.db_path, link_entities=["Maya"])
        self.assertEqual(result["total_chunks"], 1)
        records = sorted((self.vault / "knowledge").rglob("*.md"))
        self.assertEqual(len(records), 1)
        fm = load_markdown(records[0]).frontmatter
        self.assertEqual(fm["source_document"], "SDP Training Manual")
        self.assertEqual(fm["source_section"], "Budget Authority")
        self.assertIn("entity.maya", fm["links"])
        self.assertTrue(any(link.startswith("entity.organization.") for link in fm["links"]))
        self.assertEqual(fm["review_after"][:4], "2027")

        orgs = list((self.vault / "entities" / "organizations").glob("*.md"))
        self.assertTrue(orgs)

    def test_reference_ingest_replace_rewrites_existing_chunks(self) -> None:
        doc = self.src / "policy.md"
        doc.write_text("# Policy Guide\n\n## Section One\n\nInitial text.\n", encoding="utf-8")
        first = ingest_reference_sources([doc], vault=self.vault, db_path=self.db_path)
        self.assertEqual(first["total_chunks"], 1)

        doc.write_text("# Policy Guide\n\n## Section One\n\nReplaced text.\n", encoding="utf-8")
        second = ingest_reference_sources([doc], vault=self.vault, db_path=self.db_path, replace=True)
        self.assertTrue(second["replaced_files"])
        knowledge_docs = [load_markdown(path) for path in (self.vault / "knowledge").rglob("*.md")]
        self.assertEqual(len(knowledge_docs), 1)
        self.assertIn("Replaced text.", knowledge_docs[0].body)

    def test_reference_plan_reports_detected_entities(self) -> None:
        new_entity(self.vault, "Maya", subtype="person", summary="Maya is known in the vault.")
        doc = self.src / "manual.md"
        doc.write_text("# Manual\n\n## Section One\n\nMaya meets the Regional Center.\n", encoding="utf-8")

        plan = ingest_reference_sources([doc], vault=self.vault, db_path=self.db_path, link_entities=["Maya"], plan_only=True)
        self.assertTrue(plan["plan_only"])
        self.assertEqual(plan["total_chunks"], 1)
        self.assertTrue(plan["documents"][0]["detected_entity_ids"])
        self.assertTrue(plan["documents"][0]["would_create_entities"])

    def test_pdf_extraction_marks_pages_and_warns_for_sparse_text(self) -> None:
        class FakePage:
            def __init__(self, text: str):
                self._text = text

            def get_text(self, kind: str) -> str:
                return self._text

        class FakeDoc:
            def __len__(self) -> int:
                return 2

            def __getitem__(self, index: int) -> FakePage:
                return FakePage("A" if index == 0 else "B")

        fake_fitz = types.SimpleNamespace(open=lambda _: FakeDoc())
        with patch.dict("sys.modules", {"fitz": fake_fitz}):
            text, warnings = _extract_pdf_text(self.src / "sample.pdf")
        self.assertIn("--- Page 1 ---", text)
        self.assertIn("--- Page 2 ---", text)
        self.assertTrue(warnings)
