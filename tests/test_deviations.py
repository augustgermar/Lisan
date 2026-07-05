"""The deviation-sourced drive: inward-pointing, bounded, satiable.

Uses the invented cast only (privacy hard rule)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.deviations import detect, scan_deviations


def _entity(vault: Path, stem: str, name: str, kind: str, *, body: str = "", significance: str = "low", updated: str | None = None) -> Path:
    folder = vault / "entities" / ("people" if kind == "person" else "things")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    fm = {
        "id": f"entity.{stem}", "type": "entity", "canonical_name": name, "kind": kind,
        "significance": significance, "created": "2026-06-01",
        "updated": updated or "2026-07-05", "links": [],
    }
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

    def _index(self):
        from lisan.tools.rebuild_index import rebuild_index

        rebuild_index(self.vault, db_path=self.db)


class DetectorTests(_Env):
    def test_cross_kind_duplicate_detected(self):
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")
        found = [d for d in detect(self.vault) if d["klass"] == "cross_kind"]
        self.assertEqual(len(found), 1)
        self.assertIn("fragmented", found[0]["summary"])

    def test_dangling_link_detected(self):
        _entity(self.vault, "ruth", "Ruth Varga", "person")
        path = self.vault / "entities" / "people" / "ruth.md"
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm["links"] = ["drafts/2026-01-01-never-written.md"]
        path.write_text(dump_markdown(fm, doc.body), encoding="utf-8")
        found = [d for d in detect(self.vault) if d["klass"] == "dangling"]
        self.assertEqual(len(found), 1)
        self.assertIn("never resolved", found[0]["summary"])

    def test_thin_person_needs_recurring_mentions(self):
        _entity(self.vault, "dana", "Dana Feld", "person", body="A person.")
        for i in range(3):
            d = self.vault / "episodes" / f"2026-07-0{i+1}-dana-{i}.md"
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(dump_markdown(
                {"id": f"episode.dana-{i}", "type": "episode", "created": "2026-07-01",
                 "updated": "2026-07-01", "summary": f"Talked with Dana Feld about the garden, part {i}."},
                f"# Episode {i}\n\nDana Feld came up again.\n"), encoding="utf-8")
        self._index()
        found = [d for d in detect(self.vault, db_path=self.db) if d["klass"] == "thin"]
        self.assertEqual(len(found), 1)
        self.assertIn("Dana Feld", found[0]["summary"])
        # a thin person nobody mentions does NOT ache — boundedness
        _entity(self.vault, "silent", "Silent Stranger", "person", body="A person.")
        self._index()
        fps = {d["fingerprint"] for d in detect(self.vault, db_path=self.db)}
        self.assertNotIn("thin-silent-stranger", fps)

    def test_stale_high_significance_detected(self):
        old = (date.today() - timedelta(days=45)).isoformat()
        _entity(self.vault, "homestead", "The Homestead", "place", significance="high", updated=old)
        _entity(self.vault, "fresh", "Fresh Thing", "place", significance="high")
        found = [d for d in detect(self.vault) if d["klass"] == "stale"]
        self.assertEqual(len(found), 1)
        self.assertIn("Homestead", found[0]["summary"])

    def test_interoception_failed_jobs(self):
        self._index()
        conn = sqlite3.connect(self.db)
        for _ in range(6):
            conn.execute("INSERT INTO jobs (job_type, status, payload_json, priority, attempts, max_attempts, created_at) "
                         "VALUES ('x', 'failed', '{}', 50, 1, 1, '2026-07-05T00:00:00Z')")
        conn.commit(); conn.close()
        found = [d for d in detect(self.vault, db_path=self.db) if d["klass"] == "interocept"]
        self.assertTrue(any("machinery" in d["summary"] for d in found))


class EmissionTests(_Env):
    def test_emitted_loop_is_first_class_and_agent_owned(self):
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")
        result = scan_deviations(self.vault, db_path=self.db)
        self.assertEqual(result["emitted"], 1)
        loops = list((self.vault / "open_loops").glob("*.md"))
        self.assertEqual(len(loops), 1)
        fm = dict(load_markdown(loops[0]).frontmatter)
        self.assertEqual(fm["origin"], "self")
        self.assertEqual(fm["owner"], "agent")
        self.assertEqual(fm["status"], "active")
        # flows through the existing scorer and question phrasing unchanged
        from lisan.tools.drive import loop_score, phrase_question

        self.assertGreater(loop_score(fm), 0.0)
        q = phrase_question(fm)
        self.assertTrue(q.endswith("?"))
        self.assertIn("note of my own", q)  # agent-owned attribution

    def test_idempotent_rescans_never_refile(self):
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")
        scan_deviations(self.vault, db_path=self.db)
        again = scan_deviations(self.vault, db_path=self.db)
        self.assertEqual(again["emitted"], 0)
        self.assertEqual(len(list((self.vault / "open_loops").glob("*.md"))), 1)

    def test_daily_cap_bounds_the_appetite(self):
        # four distinct cross-kind deviations, cap of 2 → exactly 2 loops
        for stem in ("alpha", "bravo", "carol", "delta"):
            _entity(self.vault, f"{stem}-place", stem.title(), "place")
            _entity(self.vault, f"{stem}-person", stem.title(), "person")
        result = scan_deviations(self.vault, db_path=self.db, config={"deviations": {"daily_cap": 2}})
        self.assertGreaterEqual(result["detected"], 4)
        self.assertEqual(result["emitted"], 2)

    def test_resolved_loop_is_not_refiled_while_deviation_persists(self):
        """The owner answered; the ache must not come back the next day."""
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")
        scan_deviations(self.vault, db_path=self.db)
        loop = next((self.vault / "open_loops").glob("*.md"))
        doc = load_markdown(loop)
        loop.write_text(dump_markdown({**dict(doc.frontmatter), "status": "resolved"}, doc.body), encoding="utf-8")
        again = scan_deviations(self.vault, db_path=self.db)
        self.assertEqual(again["emitted"], 0)


class SatiationTests(_Env):
    def test_healed_deviation_closes_its_own_loop(self):
        _entity(self.vault, "larkspur-place", "Larkspur", "place")
        _entity(self.vault, "larkspur-person", "Larkspur", "person")
        scan_deviations(self.vault, db_path=self.db)
        # the fragmentation gets fixed (one entity archived)
        (self.vault / "entities" / "people" / "larkspur-person.md").unlink()
        result = scan_deviations(self.vault, db_path=self.db)
        self.assertEqual(result["satiated"], 1)
        fms = [dict(load_markdown(p).frontmatter) for p in (self.vault / "open_loops").glob("*.md")]
        fm = next(f for f in fms if f.get("deviation_fingerprint") == "cross-kind-larkspur")
        self.assertEqual(fm["status"], "resolved")
        self.assertEqual(fm["resolved_by"], "deviation.scan")
        # and resolving it must not spawn a follow-up ache about itself
        self.assertEqual(result["emitted"], 0)


class PolicyTests(_Env):
    def test_person_enrichment_is_structurally_unreachable(self):
        from lisan.tools.action_policy import action_allowed

        for tier in (0, 1, 2, 99):
            self.assertFalse(action_allowed("enrich_person", {"drive": {"action_tier": tier}}))

    def test_entity_enrichment_requires_deliberate_tier_raise(self):
        from lisan.tools.action_policy import action_allowed

        self.assertFalse(action_allowed("enrich_entity", None))  # default tier 0
        self.assertFalse(action_allowed("enrich_entity", {"drive": {"action_tier": 1}}))
        self.assertTrue(action_allowed("enrich_entity", {"drive": {"action_tier": 2}}))


if __name__ == "__main__":
    unittest.main()
