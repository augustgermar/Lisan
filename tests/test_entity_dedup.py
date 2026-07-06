"""Suffix-fragment prevention at birth + safe merge for existing fragments.
Invented cast only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.entity_merge import dedup_candidates, merge_entities
from lisan.tools.entity_resolution import _qualifier_base, _suffix_fragment_target


def _entity(vault: Path, stem: str, name: str, kind: str = "project", *, body: str = "", log=None) -> Path:
    folder = vault / "entities" / "things"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    fm = {"id": f"entity.{stem}", "type": "entity", "canonical_name": name, "kind": kind,
          "subtype": kind, "created": "2026-06-01", "updated": "2026-07-01", "aliases": [], "links": []}
    if log:
        fm["source_log"] = log
    path.write_text(dump_markdown(fm, f"# {name}\n\n{body}\n"), encoding="utf-8")
    return path


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db = self.root / "lisan.sqlite"

    def tearDown(self):
        self.tmp.cleanup()


class PreventionTests(_Env):
    def test_qualifier_base_strips_decoration_only(self):
        self.assertEqual(_qualifier_base("Deck Rebuild (summer 2026)"), "deck rebuild")
        self.assertEqual(_qualifier_base("Radio Station work day on 2026-07-11"), "radio station work day")
        self.assertEqual(_qualifier_base("Monterey Bay Aquarium"), "monterey bay aquarium")

    def test_suffix_variant_binds_to_base(self):
        index = {"deck rebuild": {"kind": "full", "path": Path("/x/deck-rebuild.md"), "canonical": "Deck Rebuild"}}
        hit = _suffix_fragment_target("Deck Rebuild project (summer 2026)", index)
        self.assertEqual(hit, Path("/x/deck-rebuild.md"))

    def test_real_compound_names_never_bind_to_prefix(self):
        """'Monterey Bay Aquarium' is a different thing than 'Monterey'."""
        index = {"monterey": {"kind": "full", "path": Path("/x/monterey.md"), "canonical": "Monterey"}}
        self.assertIsNone(_suffix_fragment_target("Monterey Bay Aquarium", index))
        self.assertIsNone(_suffix_fragment_target("Monterey Jazz Festival", index))


class MergeTests(_Env):
    def test_merge_absorbs_content_names_and_archives(self):
        keep = _entity(self.vault, "deck-rebuild", "Deck Rebuild",
                       body="The rebuild started in May.",
                       log=[{"date": "2026-06-01", "text": "planning", "folded": True}])
        frag = _entity(self.vault, "deck-rebuild-project-summer",
                       "Deck Rebuild project (summer 2026)",
                       body="Lumber delivered. Vee is helping on weekends.",
                       log=[{"date": "2026-07-01", "text": "lumber came", "folded": True}])

        result = merge_entities(self.vault, "Deck Rebuild project (summer 2026)", "Deck Rebuild", db_path=self.db)
        self.assertTrue(result["merged"])
        self.assertFalse(frag.exists())
        archived = list((self.vault / "archive" / "entities").glob("merged-*.md"))
        self.assertEqual(len(archived), 1)

        fm = load_markdown(keep).frontmatter
        self.assertIn("Deck Rebuild project (summer 2026)", fm["aliases"])
        texts = " ".join(e["text"] for e in fm["source_log"])
        self.assertIn("Lumber delivered", texts)   # narrative absorbed
        self.assertIn("lumber came", texts)        # log entries carried
        unfolded = [e for e in fm["source_log"] if not e.get("folded")]
        self.assertGreaterEqual(len(unfolded), 1)  # compaction has material
        from lisan.tools.jobs import list_jobs

        jobs = [j for j in list_jobs(db_path=self.db) if j["job_type"] == "entity.rewrite_story"]
        self.assertEqual(len(jobs), 1)             # one reweave queued

    def test_merge_refuses_missing_and_identity(self):
        _entity(self.vault, "a", "Alpha")
        self.assertFalse(merge_entities(self.vault, "Alpha", "Alpha", db_path=self.db)["merged"])
        self.assertFalse(merge_entities(self.vault, "Ghost", "Alpha", db_path=self.db)["merged"])

    def test_merge_resolves_by_stem_too(self):
        _entity(self.vault, "radio", "Community Radio Station")
        _entity(self.vault, "radio-work-day", "Community Radio Station work day")
        result = merge_entities(self.vault, "radio-work-day", "Community Radio Station", db_path=self.db)
        self.assertTrue(result["merged"])


class DedupCandidateTests(_Env):
    def test_candidates_are_same_kind_only(self):
        _entity(self.vault, "deck", "Deck Rebuild", "project")
        _entity(self.vault, "deck2", "Deck Rebuild project (summer 2026)", "project")
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")  # cross-kind: not ours
        cands = dedup_candidates(self.vault)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["keep"], "Deck Rebuild")

    def test_near_dup_becomes_a_question_loop(self):
        from lisan.tools.deviations import scan_deviations
        from lisan.tools.drive import phrase_question

        _entity(self.vault, "deck", "Deck Rebuild", "project")
        _entity(self.vault, "deck2", "Deck Rebuild project (summer 2026)", "project")
        result = scan_deviations(self.vault, db_path=self.db)
        self.assertGreaterEqual(result["emitted"], 1)
        loop = next((self.vault / "open_loops").glob("*near-dup*.md"))
        fm = load_markdown(loop).frontmatter
        q = phrase_question(fm)
        self.assertTrue(q.endswith("?"))
        self.assertIn("merging", fm["summary"])


if __name__ == "__main__":
    unittest.main()
