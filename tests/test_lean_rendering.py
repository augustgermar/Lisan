"""Lean rendering happens at the generation layer, not by regex-stripping
the rendered text afterwards. The old post-hoc strips in conversation.py
were silently coupled to the exact rendered format — a renderer change
would have quietly brought the tokens back with no error. These tests pin
the contract on the renderer itself."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import lisan.tools.retrieval  # noqa: F401 — layers↔graph import cycle needs the package entrypoint first
from lisan.tools.retrieval_graph import _format_item_detail
from lisan.tools.retrieval_layers import RetrievalItem


def _item(record_id: str, rec_type: str, rel_path: str) -> RetrievalItem:
    return RetrievalItem(
        id=record_id, type=rec_type, path=rel_path,
        summary="a summary", score=1.0, reason="rrf:fts=1,vec=2",
    )


def _write_record(root: Path, rel: str, fm: dict, body: str = "body") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + json.dumps(fm) + "\n---\n\n" + body + "\n", encoding="utf-8")
    return path


class LeanFormatterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _claim(self) -> tuple[RetrievalItem, Path]:
        fm = {
            "id": "claim.x", "type": "claim", "claim_text": "the fact",
            "claim_class": "observation", "owner": "user", "status": "active",
            "confidence": 0.8, "supporting_evidence": [], "contradicting_evidence": [],
        }
        item = _item("claim.x", "claim", "claims/x.md")
        return item, _write_record(self.root, "claims/x.md", fm)

    def test_lean_claim_omits_diagnostics_and_default_noise(self):
        item, path = self._claim()
        out = _format_item_detail(item, path, lean=True)
        self.assertNotIn("reason:", out)
        self.assertNotIn("rrf", out)
        self.assertNotIn("status: active", out)
        self.assertNotIn("supporting_evidence", out)   # empty → omitted
        self.assertNotIn("contradicting_evidence", out)
        self.assertIn("claim_text: the fact", out)     # substance stays
        self.assertIn("confidence: 0.8", out)

    def test_full_mode_keeps_everything(self):
        item, path = self._claim()
        out = _format_item_detail(item, path, lean=False)
        self.assertIn("reason: rrf:fts=1,vec=2", out)
        self.assertIn("status: active", out)
        self.assertIn("supporting_evidence: none", out)

    def test_lean_claim_keeps_non_default_status_and_real_evidence(self):
        fm = {
            "id": "claim.y", "type": "claim", "claim_text": "old fact",
            "status": "superseded", "supporting_evidence": ["evidence.a"],
        }
        item = _item("claim.y", "claim", "claims/y.md")
        path = _write_record(self.root, "claims/y.md", fm)
        out = _format_item_detail(item, path, lean=True)
        self.assertIn("status: superseded", out)       # non-default: signal
        self.assertIn("supporting_evidence: evidence.a", out)

    def test_lean_knowledge_keeps_source_document_drops_chunk_provenance(self):
        fm = {
            "id": "knowledge.k", "type": "knowledge", "summary": "s",
            "source_document": "SDP Training Manual", "source_section": "4.2",
            "source_ref": "ref", "chunk_index": 3, "total_chunks": 9,
        }
        item = _item("knowledge.k", "knowledge", "knowledge/k.md")
        path = _write_record(self.root, "knowledge/k.md", fm)
        out = _format_item_detail(item, path, lean=True)
        self.assertIn("source_document: SDP Training Manual", out)
        for gone in ("source_section", "source_ref", "chunk_index", "total_chunks", "reason:"):
            self.assertNotIn(gone, out)

    def test_lean_fallback_line_has_no_scoring_tail(self):
        item = _item("x", "unknown_type", "nowhere/x.md")
        out = _format_item_detail(item, self.root / "nowhere/x.md", lean=True)
        self.assertNotIn("rrf", out)
        self.assertIn("`x`", out)

    def test_conversation_layer_no_longer_regex_strips(self):
        """The coupling this replaces must not come back."""
        source = (Path(__file__).resolve().parents[1] / "lisan" / "tools" / "conversation.py").read_text()
        self.assertNotIn("_re.sub", source)
        self.assertNotIn("reason: .*", source)


if __name__ == "__main__":
    unittest.main()
