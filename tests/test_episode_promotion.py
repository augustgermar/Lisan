"""Episode promotion: the Writer's structured sections become a SPEC-shaped
episode record that validates; the pipeline promotes skeptic-approved
episode drafts automatically; the backlog resolver promotes what history
left behind and touches nothing else."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.draft_backlog import promote_backlog
from lisan.tools.drafts import promote_episode_from_writer, writer_output_from_draft
from lisan.tools.validator import validate_vault

WRITER = {
    "record_type": "episode",
    "summary": "Ruth and the user planted the first three apple trees at the Homestead.",
    "significance": "medium",
    "significance_rationale": "",
    "frontmatter": {
        "summary": "Ruth and the user planted the first three apple trees at the Homestead.",
        "significance": "medium",
        "confidence": 0.9,
        "confidence_basis": "First-person account of a completed activity.",
        "links": [],
        "review_after": "2026-08-01",
    },
    "sections": {
        "event_timeline": [
            {"label": "morning", "text": "Ruth arrived with the saplings at nine."},
            {"label": "afternoon", "text": "Three trees were in the ground by two."},
        ],
        "documented_evidence": [{"label": "photo", "text": "The user mentioned taking a photo of the row."}],
        "user_reported_context": [{"label": "why", "text": "The orchard is a five-year project."}],
        "interpretations": [],
        "operational_consequences": [{"label": "watering", "text": "Weekly watering through the first summer."}],
        "open_questions": [],
    },
    "questions": ["Which varieties were planted?"],
    "claims_to_create": [],
}


def _make_vault(root: Path) -> Path:
    vault = root / "vault"
    for sub in ("drafts", "episodes"):
        (vault / sub).mkdir(parents=True)
    return vault


def _write_draft(vault: Path, name: str, *, task: str = "episode", status: str = "fanout_applied",
                 approved: bool = True, writer: dict | None = None) -> Path:
    path = vault / "drafts" / name
    fm = {
        "id": f"draft.memory.{name[:20]}",
        "type": "draft",
        "created": "2026-05-22",  # deliberately stale: the filename date must win
        "updated": "2026-07-03",
        "status": status,
        "significance": "medium",
        "summary": (writer or WRITER)["summary"],
        "links": [],
        "pipeline": {"action": "lightweight", "mode": "extraction", "task": task},
        "skeptic_approved": approved,
    }
    body = "# Memory Draft\n\n## Writer\n\n```json\n" + json.dumps(writer or WRITER, indent=1) + "\n```\n"
    write_markdown(path, fm, body)
    return path


class PromoterTests(unittest.TestCase):
    def test_writer_sections_become_a_valid_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp))
            draft = _write_draft(vault, "2026-07-03-abc-planted-trees.md")
            episode = promote_episode_from_writer(
                vault, writer=WRITER, draft_path=draft, created="2026-07-03"
            )
            self.assertIsNotNone(episode)
            doc = load_markdown(episode)
            self.assertEqual(doc.frontmatter["type"], "episode")
            self.assertEqual(doc.frontmatter["created"], "2026-07-03")
            for heading in ("## Event Timeline", "## Documented Evidence", "## User-Reported Context",
                            "## Interpretations", "## Operational Consequences", "## Open Questions"):
                self.assertIn(heading, doc.body)
            self.assertIn("Ruth arrived with the saplings", doc.body)
            self.assertIn("Which varieties were planted?", doc.body)  # questions fill open_questions
            self.assertIn("drafts/2026-07-03-abc-planted-trees.md", doc.frontmatter["links"])
            self.assertEqual(doc.frontmatter["confidence"], "high")  # 0.9 maps up

    def test_promotion_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp))
            draft = _write_draft(vault, "2026-07-03-abc-planted-trees.md")
            first = promote_episode_from_writer(vault, writer=WRITER, draft_path=draft, created="2026-07-03")
            second = promote_episode_from_writer(vault, writer=WRITER, draft_path=draft, created="2026-07-03")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(len(list((vault / "episodes").glob("*.md"))), 1)

    def test_prose_sections_and_bad_dates_are_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp))
            messy = dict(WRITER, summary="A story with prose sections.")
            messy["frontmatter"] = dict(WRITER["frontmatter"], review_after="after the next ingest attempt")
            messy["sections"] = dict(WRITER["sections"],
                                     interpretations="The user treats the cafe as a grounding ritual.")
            draft = _write_draft(vault, "2026-07-03-mmm-messy.md", writer=messy)
            episode = promote_episode_from_writer(vault, writer=messy, draft_path=draft, created="2026-07-03")
            doc = load_markdown(episode)
            self.assertIn("grounding ritual", doc.body)
            self.assertNotIn("\n- T\n", doc.body)  # a string never iterates character-wise
            from datetime import date

            date.fromisoformat(doc.frontmatter["review_after"])  # must parse

    def test_long_summaries_get_bounded_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp))
            long_writer = dict(WRITER, summary="the user " + "recounted a very long story " * 20)
            draft = _write_draft(vault, "2026-07-03-zzz-long.md", writer=long_writer)
            episode = promote_episode_from_writer(vault, writer=long_writer, draft_path=draft, created="2026-07-03")
            self.assertIsNotNone(episode)
            self.assertLessEqual(len(episode.name), 100)

    def test_writer_json_roundtrips_from_draft_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp))
            draft = _write_draft(vault, "2026-07-03-abc-planted-trees.md")
            parsed = writer_output_from_draft(load_markdown(draft))
            self.assertEqual(parsed["summary"], WRITER["summary"])


class BacklogTests(unittest.TestCase):
    def test_backlog_promotes_episode_drafts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            _write_draft(vault, "2026-07-03-aaa-planted-trees.md")
            other = dict(WRITER, summary="The fence decision was recorded.")
            _write_draft(vault, "2026-07-03-bbb-fence.md", task="decision", writer=other)
            _write_draft(vault, "2026-07-03-ccc-pending.md", status="pending", approved=False,
                         writer=dict(WRITER, summary="An unreviewed story."))
            stats = promote_backlog(vault, root / "lisan.sqlite")
            self.assertEqual(stats["promoted"], 1)
            self.assertEqual(stats["skipped"], 2)  # decision draft + pending draft untouched
            episodes = list((vault / "episodes").glob("*.md"))
            self.assertEqual(len(episodes), 1)
            # Filename date wins over the stale frontmatter 'created'.
            self.assertTrue(episodes[0].name.startswith("2026-07-03"))
            draft_fm = load_markdown(vault / "drafts" / "2026-07-03-aaa-planted-trees.md").frontmatter
            self.assertEqual(draft_fm["status"], "promoted")
            self.assertIn("episodes/", draft_fm["promoted_to"])

    def test_backlog_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            _write_draft(vault, "2026-07-03-aaa-planted-trees.md")
            first = promote_backlog(vault, root / "lisan.sqlite")
            second = promote_backlog(vault, root / "lisan.sqlite")
            self.assertEqual(first["promoted"], 1)
            self.assertEqual(second["promoted"], 0)
            self.assertEqual(len(list((vault / "episodes").glob("*.md"))), 1)

    def test_promoted_episode_passes_the_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _make_vault(root)
            _write_draft(vault, "2026-07-03-aaa-planted-trees.md")
            promote_backlog(vault, root / "lisan.sqlite")
            report = validate_vault(vault)
            episode_errors = [i for i in report.issues
                              if i.severity == "error" and "episodes/" in str(i.path)]
            self.assertEqual(episode_errors, [], [str(i.message) for i in episode_errors])


if __name__ == "__main__":
    unittest.main()
