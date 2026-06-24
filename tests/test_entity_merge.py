"""Regression tests for entity merge logic (Findings #4 and #5)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.tools.memory_pipeline import (
    _create_entity_stubs,
    _entity_nickname,
    _load_entity_index,
    _looks_like_entity,
    _match_existing_entity,
    _scan_user_stated_handle,
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
        primer = frozenset({"August", "August Morgan"})
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
            _seed_primer(vault, "# Identity\n\nAugust Morgan, lead developer.\n")
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

    def test_same_first_name_collision_assigns_unique_characteristic_nicknames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            writer_out = {
                "entities_to_create": [
                    {"name": "Matt Fidler", "subtype": "person", "summary": "Matt Fidler records music at the studio."},
                    {"name": "Matt Forester", "subtype": "person", "summary": "Matt Forester handles the budget and quarterly planning."},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text="")
            files = sorted((vault / "entities" / "people").glob("*.md"))
            self.assertEqual(len(files), 2)
            frontmatters = [load_markdown(path).frontmatter for path in files]
            nicknames = [str(fm.get("nickname") or "").strip() for fm in frontmatters]
            self.assertTrue(all(nicknames))
            self.assertEqual(len(set(nicknames)), 2)
            self.assertTrue(all(not nickname[-1].isdigit() for nickname in nicknames))
            self.assertTrue(any("studio" in nickname.lower() or "music" in nickname.lower() for nickname in nicknames))
            self.assertTrue(any("budget" in nickname.lower() or "planning" in nickname.lower() for nickname in nicknames))
            self.assertEqual({fm["canonical_name"] for fm in frontmatters}, {"Matt Fidler", "Matt Forester"})
            self.assertTrue(all("nickname" in fm for fm in frontmatters))


class NicknameStopwordsTests(unittest.TestCase):
    """D1a: deixis role tokens must never appear as nickname roots."""

    def test_principal_token_not_in_nickname(self) -> None:
        # summary contains "{{principal}}" → regex strips braces → "principal"
        # which must be in _NICKNAME_STOPWORDS so it's never used as a root
        name = "Mary Kowalczyk"
        summary = "{{principal}} met Mary Kowalczyk through a mutual friend."
        # Two Marys needed to trigger collision → nickname generation
        existing = {"mary-kowalczyk"}
        nickname = _entity_nickname(name, summary=summary, source_text=summary, existing_handles=existing)
        if nickname:
            self.assertNotIn("Principal", nickname, "deixis token leaked into nickname root")

    def test_self_token_not_in_nickname(self) -> None:
        summary = "{{self}} recorded notes about Mary Flannery after {{principal}} described her."
        existing = {"mary-flannery"}
        nickname = _entity_nickname("Mary Flannery", summary=summary, source_text=summary, existing_handles=existing)
        if nickname:
            self.assertNotIn("Self", nickname)
            self.assertNotIn("User", nickname)


class UserStatedHandleTests(unittest.TestCase):
    """D1b: user-declared nicknames take priority over system-coined ones."""

    def test_scan_detects_i_call_her_pattern(self) -> None:
        text = "Went on a second date with Mary. I call her Old Fashioned because she always orders one."
        handle = _scan_user_stated_handle("Mary Kowalczyk", text, set())
        self.assertEqual(handle, "Old Fashioned")

    def test_scan_detects_been_calling_pattern(self) -> None:
        text = "Mary Flannery is intense at the gym. I've been calling her Swole Mary."
        handle = _scan_user_stated_handle("Mary Flannery", text, set())
        self.assertEqual(handle, "Swole Mary")

    def test_scan_detects_goes_by_pattern(self) -> None:
        text = "Met Mary McGrath tonight. She goes by Mystic Mary — does tarot readings."
        handle = _scan_user_stated_handle("Mary McGrath", text, set())
        self.assertEqual(handle, "Mystic Mary")

    def test_scan_returns_none_when_no_pattern(self) -> None:
        text = "Had coffee with Mary. She seems nice."
        handle = _scan_user_stated_handle("Mary Smith", text, set())
        self.assertIsNone(handle)

    def test_scan_skips_taken_handle(self) -> None:
        text = "I call her Old Fashioned."
        handle = _scan_user_stated_handle("Mary Kowalczyk", text, {"old fashioned"})
        self.assertIsNone(handle)

    def test_entity_nickname_prefers_user_handle_over_hint(self) -> None:
        # "gym" is in _NICKNAME_HINTS → would generate "GymMary" without D1b fix
        text = "Mary Flannery hits the gym every day. I've been calling her Swole Mary."
        existing = {"mary-flannery"}
        nickname = _entity_nickname("Mary Flannery", summary="goes to gym regularly", source_text=text, existing_handles=existing)
        self.assertEqual(nickname, "Swole Mary")

    def test_nickname_collision_via_vault(self) -> None:
        """End-to-end: user-stated handle lands on entity file when there's a collision."""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            source = (
                "Mary Kowalczyk and I grabbed drinks. I call her Old Fashioned. "
                "Also saw Mary Flannery at the gym. I've been calling her Swole Mary."
            )
            writer_out = {
                "entities_to_create": [
                    {"name": "Mary Kowalczyk", "subtype": "person",
                     "summary": "Mary Kowalczyk, met for drinks."},
                    {"name": "Mary Flannery", "subtype": "person",
                     "summary": "Mary Flannery, goes to the gym."},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text=source)
            files = list((vault / "entities" / "people").glob("*.md"))
            self.assertEqual(len(files), 2)
            nicknames = {
                str(load_markdown(p).frontmatter.get("nickname") or "").strip()
                for p in files
            }
            self.assertIn("Old Fashioned", nicknames)
            self.assertIn("Swole Mary", nicknames)


class PersonNoiseRejectTests(unittest.TestCase):
    """D2a: noise tokens (day names, apps, astrological terms) must never become persons."""

    def test_day_name_rejected_with_source_text(self) -> None:
        # Previously only rejected for empty source_text; now structural
        source = "Going out Saturday. My friend Tuesday recommended the place."
        empty = frozenset()
        self.assertFalse(_looks_like_entity("Tuesday", "person", empty, source))
        self.assertFalse(_looks_like_entity("Saturday", "person", empty, source))

    def test_dating_app_rejected_as_person(self) -> None:
        empty = frozenset()
        self.assertFalse(_looks_like_entity("Bumble", "person", empty, "Met her on Bumble."))
        self.assertFalse(_looks_like_entity("Hinge", "person", empty, "Matched on Hinge."))
        self.assertFalse(_looks_like_entity("Tinder", "person", empty, "Swiped on Tinder."))

    def test_astrological_term_rejected_as_person(self) -> None:
        empty = frozenset()
        self.assertFalse(_looks_like_entity("Mercury", "person", empty,
                                            "Mercury retrograde is messing with everything."))

    def test_noise_tokens_still_allowed_as_other_kinds(self) -> None:
        empty = frozenset()
        # Bumble as organization, Mercury as thing — non-person path is permissive
        self.assertTrue(_looks_like_entity("Bumble", "organization", empty, ""))
        self.assertTrue(_looks_like_entity("Mercury", "thing", empty, ""))

    def test_primer_known_name_bypasses_noise_check(self) -> None:
        primer = frozenset({"Mercury"})
        self.assertTrue(_looks_like_entity("Mercury", "person", primer, ""))

    def test_noise_entities_not_created_in_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            _seed_primer(vault, "# Identity\n\nDanny Callahan, structural engineer.\n")
            source = (
                "Mercury retrograde is wrecking my week. "
                "Going out Tuesday. Met someone on Bumble."
            )
            writer_out = {
                "entities_to_create": [
                    {"name": "Mercury", "kind": "person", "summary": "Mercury retrograde mentioned."},
                    {"name": "Tuesday", "kind": "person", "summary": "Tuesday mentioned."},
                    {"name": "Bumble", "kind": "person", "summary": "Dating app."},
                ],
            }
            _create_entity_stubs(vault, writer_out, draft_rel="drafts/test.md", source_text=source)
            people_dir = vault / "entities" / "people"
            created = list(people_dir.glob("*.md")) if people_dir.exists() else []
            self.assertEqual(created, [], f"Noise entities should not be created: {[p.stem for p in created]}")


if __name__ == "__main__":
    unittest.main()
