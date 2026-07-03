from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lisan.tools.entity_kind import assign_kind, classify_structural, CANONICAL_KINDS
from lisan.tools.primer_index import roster, roster_kind
from lisan.tools.record_factory import new_entity
from lisan.frontmatter import load_markdown

_CORE = """---
principal:
  name: "Sam Rivers"
  aliases: ["Sam"]
roster:
  - name: "Dana Cole"
    aliases: ["Dana"]
    kind: person
  - name: "Atlas"
    kind: project
  - name: "Houston"
    kind: place
  - name: "web-03.prod"
    aliases: ["web-03"]
    kind: system
deixis_frame: |
  frame
---

# Identity Core
"""


def _vault() -> Path:
    v = Path(tempfile.mkdtemp()) / "vault"
    (v / "primer").mkdir(parents=True)
    (v / "primer" / "identity-core.md").write_text(_CORE, encoding="utf-8")
    return v


class RosterTests(unittest.TestCase):
    def test_roster_parsed(self) -> None:
        v = _vault()
        entries = {e.name: e.kind for e in roster(v)}
        self.assertEqual(entries["Atlas"], "project")
        self.assertEqual(entries["Houston"], "place")
        self.assertEqual(entries["web-03.prod"], "system")

    def test_roster_classification_layer1(self) -> None:
        v = _vault()
        self.assertEqual(roster_kind(v, "Atlas"), "project")
        self.assertEqual(roster_kind(v, "Houston"), "place")
        self.assertEqual(roster_kind(v, "Dana"), "person")       # via alias
        self.assertEqual(roster_kind(v, "web-03"), "system")     # via alias
        self.assertIsNone(roster_kind(v, "Maren"))               # not in roster


class StructuralTests(unittest.TestCase):
    def test_structural_signals(self) -> None:
        self.assertEqual(classify_structural("10.0.3.14"), "system")
        self.assertEqual(classify_structural("web-03.prod"), "system")
        self.assertEqual(classify_structural("/etc/lisan/config.yaml"), "artifact")
        self.assertEqual(classify_structural("https://example.com/x"), "system")
        self.assertEqual(classify_structural("Anonabox LLC"), "organization")
        self.assertIsNone(classify_structural("Maren"))          # plain name: defer


class AssignKindTests(unittest.TestCase):
    def test_roster_overrides_model(self) -> None:
        v = _vault()
        # even if the model guessed person, the roster wins
        self.assertEqual(assign_kind("Atlas", v, model_kind="person"), "project")

    def test_structural_when_not_in_roster(self) -> None:
        v = _vault()
        self.assertEqual(assign_kind("10.0.0.5", v), "system")

    def test_unknown_noun_falls_to_thing_never_person(self) -> None:
        v = _vault()
        # no roster, no structural signal, no model hint -> thing, NOT person
        self.assertEqual(assign_kind("Maren", v, model_kind=""), "thing")
        self.assertEqual(assign_kind("Quetzal", v, model_kind="unknown"), "thing")

    def test_model_explicit_choice_respected(self) -> None:
        v = _vault()
        self.assertEqual(assign_kind("Maren", v, model_kind="person"), "person")


class EntityRecordTests(unittest.TestCase):
    def test_new_entity_writes_kind_field(self) -> None:
        v = _vault()
        rec = new_entity(vault=v, name="Atlas", subtype="project", summary="The redesign.")
        fm = load_markdown(rec.path).frontmatter
        self.assertEqual(fm["kind"], "project")
        self.assertEqual(fm["subtype"], "project")
        self.assertIn("entities/projects", str(rec.path))

    def test_open_set_novel_kind_accepted(self) -> None:
        v = _vault()
        # a kind not in the canonical starter set must be stored, not rejected
        self.assertNotIn("vehicle", CANONICAL_KINDS)
        rec = new_entity(vault=v, name="The Van", subtype="vehicle", summary="A van.")
        fm = load_markdown(rec.path).frontmatter
        self.assertEqual(fm["kind"], "vehicle")
        self.assertIn("entities/vehicle", str(rec.path))

    def test_kind_scoping_keeps_same_name_different_kinds_separate(self) -> None:
        v = _vault()
        person_atlas = new_entity(vault=v, name="Atlas", subtype="person", summary="A person.")
        project_atlas = new_entity(vault=v, name="Atlas", subtype="project", summary="A project.")
        # different kinds => different records/paths, never merged
        self.assertNotEqual(person_atlas.path, project_atlas.path)
        self.assertIn("entities/people", str(person_atlas.path))
        self.assertIn("entities/projects", str(project_atlas.path))


if __name__ == "__main__":
    unittest.main()


class KindContextLeakTests(unittest.TestCase):
    """Kind describes what the entity IS, never what the turn was about: a
    person mentioned near the word 'birthday' must not become an event."""

    def test_event_words_in_context_do_not_leak(self):
        from lisan.tools.entity_kind import classify_structural

        self.assertIsNone(classify_structural("Maya", context="Birthdays roster: Maya turns 8"))
        self.assertIsNone(classify_structural("Ruth", context="dentist appointment for the kids"))

    def test_event_words_in_the_name_still_classify(self):
        from lisan.tools.entity_kind import classify_structural

        self.assertEqual(classify_structural("Maya's Birthday Party"), "event")
        self.assertEqual(classify_structural("graduation"), "event")


class PathSegmentGateTests(unittest.TestCase):
    def test_path_segments_are_not_entities(self):
        from lisan.tools.entity_resolution import _looks_like_entity

        text = "look at /Users/august/Library/Mobile Documents/iCloud~md~obsidian/Documents/Vault01/"
        self.assertFalse(_looks_like_entity("Mobile Documents", "person", frozenset(), text))
        self.assertFalse(_looks_like_entity("Vault01", "organization", frozenset(), text))

    def test_prose_mentions_survive_even_with_paths_present(self):
        from lisan.tools.entity_resolution import _looks_like_entity

        self.assertTrue(_looks_like_entity("Maya", "person", frozenset(),
                                           "my daughter Maya: see /notes/Maya/file.md"))
