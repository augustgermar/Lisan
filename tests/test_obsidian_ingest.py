"""Obsidian-aware ingestion: wikilinks become prose + graph, config junk is
skipped, sources are never modified, and the chat tool gates on approval."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.ingest import (
    _load_reference_document,
    _reference_files_in_directory,
    _resolve_wikilinks,
    ingest_reference_sources,
)


class WikilinkTests(unittest.TestCase):
    def test_all_wikilink_forms(self):
        text, targets = _resolve_wikilinks(
            "Saw [[Ruth Varga|Ruth]] at [[the Larkspur Cafe]]. "
            "Notes in [[Projects#Greenhouse]]. ![[sketch.png]] Later [[Ruth Varga]] again."
        )
        self.assertEqual(text.split(), "Saw Ruth at the Larkspur Cafe. Notes in Projects. Later Ruth Varga again.".split())
        self.assertEqual(targets, ["Ruth Varga", "the Larkspur Cafe", "Projects"])

    def test_plain_text_untouched(self):
        text, targets = _resolve_wikilinks("No links here, just [brackets] and [markdown](links).")
        self.assertEqual(targets, [])
        self.assertIn("[markdown](links)", text)


class _ObsidianVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"
        self.source = self.root / "obsidian-vault"
        (self.source / ".obsidian").mkdir(parents=True)
        (self.source / ".obsidian" / "app.json").write_text('{"theme": "dark"}', encoding="utf-8")
        (self.source / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
        note = self.source / "Ruth Varga.md"
        note.write_text(
            "---\ntags: [people, garden]\n---\n\n# Ruth Varga\n\n"
            "Ruth runs [[the Larkspur Cafe]] and helps with [[Greenhouse Rebuild|the greenhouse]].\n",
            encoding="utf-8",
        )
        self.note = note
        self.note_bytes = note.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()


class DirectoryScanTests(_ObsidianVault):
    def test_dot_directories_are_never_ingested(self):
        files = _reference_files_in_directory(self.source)
        self.assertEqual([f.name for f in files], ["Ruth Varga.md"])

    def test_loaded_document_carries_graph_and_clean_text(self):
        doc = _load_reference_document(self.note)
        self.assertNotIn("[[", doc["text"])
        self.assertIn("the greenhouse", doc["text"])
        self.assertEqual(doc["wikilinks"], ["the Larkspur Cafe", "Greenhouse Rebuild"])
        self.assertEqual(doc["tags"], ["garden", "people"])


class EndToEndTests(_ObsidianVault):
    def test_ingest_preserves_graph_and_never_touches_source(self):
        result = ingest_reference_sources([self.source], vault=self.vault, db_path=self.db)
        created = result.get("created_records") or []
        self.assertGreaterEqual(len(created), 1)
        record = load_markdown(Path(created[0]["path"]))
        self.assertNotIn("[[", record.body)
        self.assertEqual(record.frontmatter.get("source_wikilinks"), ["Greenhouse Rebuild", "the Larkspur Cafe"])
        self.assertEqual(record.frontmatter.get("source_tags"), ["garden", "people"])
        # the source file is byte-identical — reads only, never writes
        self.assertEqual(self.note.read_bytes(), self.note_bytes)
        # and the obsidian config never became a record
        titles = " ".join(str(r.get("title")) for r in created)
        self.assertNotIn("app", titles)
        self.assertNotIn("workspace", titles)


class ToolTests(_ObsidianVault):
    def _tool(self, approval_fn):
        from lisan.tools.execution_tools import ingest_files_tool

        return lambda **kw: ingest_files_tool(vault=self.vault, db_path=self.db, approval_fn=approval_fn, **kw)

    def test_denied_approval_writes_nothing(self):
        out = self._tool(lambda n, a: False)(path=str(self.source))
        self.assertIn("denied", out)
        knowledge = list((self.vault / "knowledge").rglob("*.md"))
        self.assertEqual(knowledge, [])

    def test_approval_sees_counts_then_ingests(self):
        seen: list[dict] = []

        def approve(name, args):
            seen.append({"name": name, **args})
            return True

        out = self._tool(approve)(path=str(self.source))
        self.assertIn("Assimilated 1 file(s)", out)
        self.assertEqual(seen[0]["name"], "ingest_files")
        self.assertIn("1 file(s)", seen[0]["task"])  # counts shown at the veto point
        # knowledge mode keeps the old flat behavior
        out2 = self._tool(approve)(path=str(self.source), mode="knowledge", replace=True)
        self.assertIn("Ingested 1 file(s)", out2)

    def test_missing_path_is_a_plain_error(self):
        out = self._tool(lambda n, a: True)(path=str(self.source / "nope"))
        self.assertIn("does not exist", out)


if __name__ == "__main__":
    unittest.main()
