"""Life ingestion against a vault as disorganized as the real reference one:
no frontmatter, no wikilinks, lowercase-slug and username filenames, folder
tree as the only signal, root junk, empty notes, a principal self-note.
Invented cast only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.ingest_life import _deslug, ingest_life_sources


def _messy_vault(root: Path) -> Path:
    src = root / "notes-vault"
    (src / ".obsidian").mkdir(parents=True)
    (src / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    people = src / "Relational" / "People"
    people.mkdir(parents=True)
    # lowercase slug person note, no frontmatter
    (people / "ruth-varga.md").write_text(
        "Ruth is my oldest friend from the co-op days. She runs the Larkspur Cafe now "
        "and is quietly generous with everyone. Went through a rough patch in 2024.",
        encoding="utf-8")
    # username-shaped person note
    (people / "moonpie77.md").write_text(
        "Met on the forum. Knows everything about fermentation. Real name unknown.",
        encoding="utf-8")
    # principal's own note — must never become a third-party entity
    (people / "vega-owner.md").write_text("Notes about myself and my goals.", encoding="utf-8")
    # empty person note
    (people / "empty-person.md").write_text("", encoding="utf-8")
    # meta notes that live in the People folder but are not people
    (people / "people-index.md").write_text("A list of everyone: see individual notes.", encoding="utf-8")
    (people / "people-template.md").write_text("Relationship: / Birthday: / Summary:", encoding="utf-8")
    # dated note at root
    (src / "2026-04-11.md").write_text("Long walk with the girls. Talked about the summer trip.", encoding="utf-8")
    # root junk
    (src / "tmp.md").write_text("random scratch content about nothing in particular", encoding="utf-8")
    (src / "Big Ideas.md").write_text("A framework for thinking about seasons of life. " * 30, encoding="utf-8")
    return src


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"
        # principal kernel so tokenization + self-note guard work
        core = self.vault / "primer" / "identity-core.md"
        core.parent.mkdir(parents=True, exist_ok=True)
        core.write_text(
            '---\nprincipal:\n  name: "Vega Owner"\n  aliases: ["Vega Owner", "Vega"]\n'
            'assistant:\n  name: "Scout"\n---\n\n# Identity Core\n', encoding="utf-8")
        self.src = _messy_vault(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class DeslugTests(unittest.TestCase):
    def test_deslug_shapes(self):
        self.assertEqual(_deslug("ruth-varga"), "Ruth Varga")
        self.assertEqual(_deslug("adrienne_mcgraw"), "Adrienne Mcgraw")
        self.assertEqual(_deslug("moonpie77"), "Moonpie77")
        self.assertEqual(_deslug("Already Cased"), "Already Cased")


class ClassificationTests(_Env):
    def test_plan_classifies_the_mess(self):
        plan = ingest_life_sources([self.src], vault=self.vault, db_path=self.db, plan_only=True)
        c = plan["classified"]
        self.assertEqual(c["entity"], 2)          # ruth + moonpie (vega routed away, empty skipped)
        self.assertEqual(c["episode"], 1)
        self.assertEqual(c["skipped_empty"], 1)
        self.assertEqual(c["knowledge"], 5)       # tmp, Big Ideas, vega-owner, index, template
        names = {e["name"] for e in plan["would_create_entities"]}
        self.assertEqual(names, {"Ruth Varga", "Moonpie77"})
        # planning writes nothing
        self.assertEqual(list((self.vault / "entities").rglob("*.md")), [])


class AssimilationTests(_Env):
    def test_full_run_builds_memory_structure(self):
        result = ingest_life_sources([self.src], vault=self.vault, db_path=self.db)

        # entities created with the right kind, log seeded, story job queued
        self.assertEqual({e["name"] for e in result["entities_created"]}, {"Ruth Varga", "Moonpie77"})
        ruth = next(p for p in (self.vault / "entities").rglob("*.md")
                    if load_markdown(p).frontmatter.get("canonical_name") == "Ruth Varga")
        fm = load_markdown(ruth).frontmatter
        self.assertEqual(fm.get("kind") or fm.get("subtype"), "person")
        log = fm.get("source_log") or []
        self.assertEqual(len(log), 1)
        self.assertIn("Larkspur Cafe", log[0]["text"])
        self.assertIn("ruth-varga", log[0]["text"])  # provenance names the note
        self.assertEqual(result["rewrite_jobs"], 2)
        from lisan.tools.jobs import list_jobs

        queued = [j for j in list_jobs(db_path=self.db) if j["job_type"] == "entity.rewrite_story"]
        self.assertGreaterEqual(len(queued), 1)
        self.assertTrue(all(j["payload"].get("force_compact") for j in queued))

        # the principal's own note became knowledge, never an entity
        names = {str(load_markdown(p).frontmatter.get("canonical_name") or "")
                 for p in (self.vault / "entities").rglob("*.md")}
        self.assertNotIn("Vega Owner", names)

        # episode from the dated note
        self.assertEqual(result["episodes_created"], 1)
        episodes = list((self.vault / "episodes").rglob("*.md"))
        self.assertEqual(len(episodes), 1)
        self.assertIn("2026-04-11", load_markdown(episodes[0]).frontmatter.get("summary", ""))

        # knowledge fallback for junk + full-text for person notes, entity-linked
        self.assertGreaterEqual(result["knowledge_records"], 4)
        ruth_id = str(fm.get("id"))
        linked = [p for p in (self.vault / "knowledge").rglob("*.md")
                  if ruth_id in (load_markdown(p).frontmatter.get("links") or [])]
        self.assertGreaterEqual(len(linked), 1)

    def test_rerun_is_idempotent(self):
        first = ingest_life_sources([self.src], vault=self.vault, db_path=self.db)
        second = ingest_life_sources([self.src], vault=self.vault, db_path=self.db)
        self.assertEqual(second["entities_created"], [])
        self.assertEqual(second["rewrite_jobs"], 0)  # same note content: no re-append
        self.assertEqual(second["episodes_created"], 0)
        ruth = next(p for p in (self.vault / "entities").rglob("*.md")
                    if load_markdown(p).frontmatter.get("canonical_name") == "Ruth Varga")
        self.assertEqual(len(load_markdown(ruth).frontmatter["source_log"]), 1)

    def test_edited_note_appends_new_log_entry(self):
        ingest_life_sources([self.src], vault=self.vault, db_path=self.db)
        note = self.src / "Relational" / "People" / "ruth-varga.md"
        note.write_text(note.read_text(encoding="utf-8") + "\nShe just got engaged.", encoding="utf-8")
        again = ingest_life_sources([self.src], vault=self.vault, db_path=self.db, replace=True)
        ruth = next(p for p in (self.vault / "entities").rglob("*.md")
                    if load_markdown(p).frontmatter.get("canonical_name") == "Ruth Varga")
        log = load_markdown(ruth).frontmatter["source_log"]
        self.assertEqual(len(log), 2)
        self.assertIn("engaged", log[1]["text"])
        self.assertEqual(again["rewrite_jobs"], 1)

    def test_life_mode_never_phrase_mints_entities(self):
        """141 junk 'organizations' on the first real run: phrase heuristics
        must stay off in life mode — entity birth belongs to the classifier."""
        (self.src / "Financial").mkdir(exist_ok=True)
        (self.src / "Financial" / "pension.md").write_text(
            "Called the Benefit Services Center about the Defined Benefit Program "
            "and the State Retirement Board.", encoding="utf-8")
        ingest_life_sources([self.src], vault=self.vault, db_path=self.db)
        names = {str(load_markdown(p).frontmatter.get("canonical_name") or "")
                 for p in (self.vault / "entities").rglob("*.md")}
        self.assertEqual(names, {"Ruth Varga", "Moonpie77"})  # people only, no phrase-orgs

    def test_sources_never_modified(self):
        before = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        ingest_life_sources([self.src], vault=self.vault, db_path=self.db)
        after = {p: p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
