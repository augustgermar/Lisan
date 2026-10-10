"""Suffix-fragment prevention at birth + safe merge for existing fragments.
Invented cast only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.entity_merge import (
    contradictory_structured_attributes,
    dedup_candidates,
    merge_entities,
)
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

        result = merge_entities(
            self.vault, "Deck Rebuild project (summer 2026)", "Deck Rebuild",
            db_path=self.db, owner_rationale="Test fixture: both records describe the same project.",
        )
        self.assertTrue(result["merged"])
        self.assertFalse(frag.exists())
        archived = list((self.vault / "archive" / "entities").glob("merged-*.md"))
        self.assertEqual(len(archived), 1)

        fm = load_markdown(keep).frontmatter
        self.assertIn("Deck Rebuild project (summer 2026)", fm["aliases"])
        texts = " ".join(e["text"] for e in fm["source_log"])
        self.assertIn("lumber came", texts)        # log entries carried
        self.assertEqual(len(fm["source_log"]), 2)  # sum of both source logs
        self.assertIn("Lumber delivered", load_markdown(archived[0]).body)
        self.assertIn("Test fixture", fm["merge_history"][-1]["rationale"])
        unfolded = [e for e in fm["source_log"] if not e.get("folded")]
        self.assertGreaterEqual(len(unfolded), 1)  # compaction has material
        from lisan.tools.jobs import list_jobs

        jobs = [j for j in list_jobs(db_path=self.db) if j["job_type"] == "entity.rewrite_story"]
        self.assertEqual(len(jobs), 1)             # one reweave queued

    def test_merge_refuses_missing_and_identity(self):
        _entity(self.vault, "a", "Alpha")
        self.assertFalse(merge_entities(self.vault, "Alpha", "Alpha", db_path=self.db)["merged"])
        self.assertFalse(merge_entities(self.vault, "Ghost", "Alpha", db_path=self.db)["merged"])
        no_rationale = merge_entities(self.vault, "Alpha", "Alpha", db_path=self.db)
        self.assertIn("owner adjudication rationale", no_rationale["reason"])

    def test_merge_resolves_by_stem_too(self):
        _entity(self.vault, "radio", "Community Radio Station")
        _entity(self.vault, "radio-work-day", "Community Radio Station work day")
        result = merge_entities(
            self.vault, "radio-work-day", "Community Radio Station", db_path=self.db,
            owner_rationale="Test fixture: both records describe the same station.",
        )
        self.assertTrue(result["merged"])

    def test_merge_refuses_explicit_owner_distinction_in_transcript(self):
        hollis = _entity(self.vault, "robert-hollis", "Robert Hollis", kind="person")
        nash = _entity(self.vault, "robert-nash", "Robert Nash", kind="person")
        transcripts = self.vault / "transcripts"
        transcripts.mkdir(parents=True, exist_ok=True)
        transcript = transcripts / "2026-07-20.md"
        transcript.write_text(
            "## Conversation\n\n"
            "USER: Robert Nash is his own person, distinct from Robert Hollis.\n",
            encoding="utf-8",
        )

        result = merge_entities(
            self.vault, "Robert Hollis", "Robert Nash", db_path=self.db,
            owner_rationale="Test fixture rationale.",
        )

        self.assertFalse(result["merged"])
        self.assertIn("owner transcript evidence", result["reason"])
        self.assertEqual(result["owner_distinction_evidence"][0]["line"], 3)
        self.assertTrue(hollis.exists() and nash.exists())
        self.assertEqual(list((self.vault / "archive" / "entities").glob("merged-*.md")), [])

    def test_merge_refuses_contradictory_birthday_attributes(self):
        mj = _entity(self.vault, "mj", "Mj", kind="person", log=[
            {"date": "2026-04-30", "text": "Birthday: May 9", "folded": True},
        ])
        nora = _entity(self.vault, "nora-castellan", "Nora Castellan", kind="person", log=[
            {"date": "2026-05-15", "text": "Born August 9", "folded": True},
        ])
        result = merge_entities(
            self.vault, "Mj", "Nora Castellan", db_path=self.db,
            owner_rationale="Test fixture rationale.",
        )
        self.assertFalse(result["merged"])
        self.assertIn("structured identity attributes conflict", result["reason"])
        self.assertTrue(mj.exists() and nora.exists())
        self.assertEqual(contradictory_structured_attributes(
            load_markdown(mj).frontmatter, load_markdown(nora).frontmatter
        )[0]["attribute"], "birthday")

    def test_merge_does_not_promote_note_title_to_person_alias(self):
        marisol = _entity(self.vault, "marisol", "Marisol", kind="person")
        team = _entity(self.vault, "team-marisol", "Team Marisol", kind="person")
        result = merge_entities(
            self.vault, "Team Marisol", "Marisol", db_path=self.db,
            owner_rationale="Test fixture: same person; title is not an identity alias.",
        )
        self.assertTrue(result["merged"])
        self.assertNotIn("Team Marisol", load_markdown(marisol).frontmatter["aliases"])
        self.assertFalse(team.exists())


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
