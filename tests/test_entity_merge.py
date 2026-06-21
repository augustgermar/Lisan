"""Regression tests for entity merge logic (Findings #4 and #5)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.frontmatter import dump_markdown
from lisan.tools.memory_pipeline import (
    _create_entity_stubs,
    _load_entity_index,
    _looks_like_entity,
    _match_existing_entity,
)


def _seed_entity(
    vault: Path,
    slug: str,
    canonical: str,
    aliases: list[str] | None = None,
    summary: str | None = None,
) -> Path:
    """Write a minimal person entity file to the vault."""
    path = vault / "entities" / "people" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": f"entity.person.{slug}",
        "type": "entity",
        "subtype": "person",
        "canonical_name": canonical,
        "aliases": aliases or [],
        "summary": summary or f"{canonical} is a person.",
    }
    path.write_text(dump_markdown(fm, f"# {canonical}\n"), encoding="utf-8")
    return path


def _seed_primer(vault: Path, identity_text: str) -> None:
    (vault / "primer").mkdir(parents=True, exist_ok=True)
    (vault / "primer" / "identity.md").write_text(identity_text, encoding="utf-8")


class LooksLikeEntityTests(unittest.TestCase):
    """Finding #4: stub validation rejects nonsense entity names."""

    def test_single_capitalized_word_rejected(self) -> None:
        empty = frozenset()
        self.assertFalse(_looks_like_entity("Slack", "person", empty))
        self.assertFalse(_looks_like_entity("Strategically", "person", empty))
        self.assertFalse(_looks_like_entity("What", "person", empty))

    def test_day_of_week_rejected(self) -> None:
        self.assertFalse(_looks_like_entity("Friday", "person", frozenset()))

    def test_month_rejected_without_primer(self) -> None:
        # "August" with empty primer is rejected — there is no allowlist.
        self.assertFalse(_looks_like_entity("August", "person", frozenset()))

    def test_month_accepted_when_in_primer(self) -> None:
        # User's name happens to be a month — primer override wins.
        primer = frozenset({"August", "August Germar"})
        self.assertTrue(_looks_like_entity("August", "person", primer))

    def test_multi_word_proper_name_accepted(self) -> None:
        self.assertTrue(_looks_like_entity("Marcus Webb", "person", frozenset()))
        self.assertTrue(_looks_like_entity("Amara Okonkwo", "person", frozenset()))

    def test_multi_word_with_stopword_token_rejected(self) -> None:
        # "Friday Smith" rejected because "Friday" is a day token.
        self.assertFalse(_looks_like_entity("Friday Smith", "person", frozenset()))

    def test_place_subtype_more_permissive(self) -> None:
        # Non-person subtypes don't enforce the multi-word rule.
        self.assertTrue(_looks_like_entity("Berkeley", "place", frozenset()))


class EntityIndexTests(unittest.TestCase):
    """Finding #5: index distinguishes full names from token-only entries."""

    def test_full_name_indexed_as_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "marcus-webb", "Marcus Webb")
            index = _load_entity_index(vault)
            self.assertEqual(index["marcus webb"]["kind"], "full")
            self.assertEqual(index["marcus"]["kind"], "token")
            self.assertEqual(index["webb"]["kind"], "token")

    def test_shared_token_becomes_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "emeka-okonkwo", "Emeka Okonkwo")
            _seed_entity(vault, "amara-okonkwo", "Amara Okonkwo")
            index = _load_entity_index(vault)
            self.assertEqual(index["okonkwo"]["kind"], "ambiguous")
            self.assertEqual(index["emeka okonkwo"]["kind"], "full")
            self.assertEqual(index["amara okonkwo"]["kind"], "full")


class MatchExistingEntityTests(unittest.TestCase):
    """Finding #5: same-surname proposals no longer merge."""

    def test_amara_does_not_merge_into_emeka(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "emeka-okonkwo", "Emeka Okonkwo")
            index = _load_entity_index(vault)
            match = _match_existing_entity(vault, "Amara Okonkwo", "person", index)
            # Single shared token ("Okonkwo") must NOT trigger a multi-word merge.
            self.assertIsNone(match)

    def test_full_name_match_still_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "marcus-webb", "Marcus Webb")
            index = _load_entity_index(vault)
            match = _match_existing_entity(vault, "Marcus Webb", "person", index)
            self.assertIsNotNone(match)

    def test_first_name_only_proposal_merges_when_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "marcus-webb", "Marcus Webb")
            index = _load_entity_index(vault)
            match = _match_existing_entity(vault, "Marcus", "person", index)
            # Single-token proposal absorbs into the unambiguous existing entity.
            self.assertIsNotNone(match)

    def test_first_name_only_proposal_refused_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(vault, "marcus-webb", "Marcus Webb")
            _seed_entity(vault, "marcus-tan", "Marcus Tan")
            index = _load_entity_index(vault)
            match = _match_existing_entity(vault, "Marcus", "person", index)
            # Ambiguous "Marcus" → refuse to pick a side.
            self.assertIsNone(match)

    def test_context_disambiguation_uses_resolver_for_same_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(
                vault,
                "matt-fidler",
                "Matt Fidler",
                aliases=["Matt"],
                summary="Matt Fidler records music at the studio.",
            )
            _seed_entity(
                vault,
                "matt-forester",
                "Matt Forester",
                aliases=["Matt"],
                summary="Matt Forester handles the budget and quarterly planning.",
            )
            index = _load_entity_index(vault)
            fake = SimpleNamespace(
                candidate={"path": vault / "entities" / "people" / "matt-fidler.md"},
                confidence=0.91,
                score=0.91,
                method="context",
            )
            with patch("lisan.tools.memory_pipeline.resolve_reference", return_value=fake) as mock_resolve:
                match = _match_existing_entity(
                    vault,
                    "Matt",
                    "person",
                    index,
                    source_text="record music with Matt at the studio",
                )
            self.assertEqual(match, vault / "entities" / "people" / "matt-fidler.md")
            mock_resolve.assert_called_once()

    def test_context_disambiguation_aggressively_splits_on_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_entity(
                vault,
                "matt-fidler",
                "Matt Fidler",
                aliases=["Matt"],
                summary="Matt Fidler records music at the studio.",
            )
            _seed_entity(
                vault,
                "matt-forester",
                "Matt Forester",
                aliases=["Matt"],
                summary="Matt Forester handles the budget and quarterly planning.",
            )
            index = _load_entity_index(vault)
            fake = SimpleNamespace(
                candidate={"path": vault / "entities" / "people" / "matt-fidler.md"},
                confidence=0.22,
                score=0.22,
                method="residue",
            )
            with patch("lisan.tools.memory_pipeline.resolve_reference", return_value=fake):
                match = _match_existing_entity(
                    vault,
                    "Matt",
                    "person",
                    index,
                    source_text="record music with Matt at the studio",
                )
            self.assertIsNone(match)

    def test_kind_scoping_keeps_project_atlas_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            project_path = vault / "entities" / "projects" / "atlas.md"
            project_path.parent.mkdir(parents=True, exist_ok=True)
            project_path.write_text(
                dump_markdown(
                    {
                        "id": "entity.project.atlas",
                        "type": "entity",
                        "subtype": "project",
                        "canonical_name": "Atlas",
                        "aliases": [],
                        "summary": "Atlas is a project.",
                    },
                    "# Atlas\n",
                ),
                encoding="utf-8",
            )
            index = _load_entity_index(vault)
            match = _match_existing_entity(vault, "Atlas", "person", index)
            self.assertIsNone(match)


class CreateEntityStubsTests(unittest.TestCase):
    """Finding #4 + #5 + #12: end-to-end fanout drops junk and preserves
    primer-known names."""

    def test_nonsense_entities_are_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "primer").mkdir()
            (vault / "primer" / "identity.md").write_text(
                "# Identity\n\nNadia Okonkwo, software engineer.\n",
                encoding="utf-8",
            )
            writer_out = {
                "entities_to_create": [
                    {"name": "Slack", "subtype": "person"},
                    {"name": "Strategically", "subtype": "person"},
                    {"name": "Friday", "subtype": "person"},
                    {"name": "What", "subtype": "person"},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text="")
            people_dir = vault / "entities" / "people"
            created = list(people_dir.glob("*.md")) if people_dir.exists() else []
            self.assertEqual(created, [], "Junk entities should not be created")

    def test_primer_known_single_name_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nAugust Germar, lead developer.\n")
            writer_out = {
                "entities_to_create": [
                    {"name": "August", "subtype": "person"},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text="")
            files = list((vault / "entities" / "people").glob("*.md"))
            self.assertEqual(len(files), 1)

    def test_amara_and_emeka_get_separate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault,
                "# Identity\n\nEmeka Okonkwo (father).\nAmara Okonkwo (sister).\n")
            writer_out = {
                "entities_to_create": [
                    {"name": "Emeka Okonkwo", "subtype": "person"},
                    {"name": "Amara Okonkwo", "subtype": "person"},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text="")
            files = sorted(p.name for p in (vault / "entities" / "people").glob("*.md"))
            self.assertEqual(len(files), 2, f"Expected 2 distinct files, got {files}")

    def test_principal_role_token_is_not_materialized_as_entity(self) -> None:
        """FIX A (2026-06-19 eval): the {{principal}}/{{self}} role tokens and
        their bare slugs must never become entity records."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nMarcus Delgado (network admin).\n")
            writer_out = {
                "entities_to_create": [
                    {"name": "{{principal}}", "subtype": "person",
                     "summary": "{{principal}} is the senior network admin."},
                    {"name": "{{self}}", "subtype": "person"},
                    {"name": "principal", "subtype": "person"},
                    {"name": "Marcus Delgado", "subtype": "person"},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text="")
            all_entities = list((vault / "entities").rglob("*.md"))
            slugs = sorted(p.stem for p in all_entities)
            # Only the real person survives; no principal/self token residue.
            self.assertEqual(slugs, ["marcus-delgado"], f"unexpected entities: {slugs}")
            self.assertFalse((vault / "entities" / "events" / "principal.md").exists())


if __name__ == "__main__":
    unittest.main()
