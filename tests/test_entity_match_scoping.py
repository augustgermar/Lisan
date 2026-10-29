"""_match_existing_entity: a conflict excludes one candidate, not all of
them; a shared token never folds across kinds."""
from __future__ import annotations

from pathlib import Path

from lisan.frontmatter import dump_markdown
from lisan.tools.entity_resolution import _load_entity_index, _match_existing_entity


def _seed(vault: Path, folder: str, slug: str, canonical: str, subtype: str, aliases=None) -> Path:
    path = vault / "entities" / folder / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": f"entity.{subtype}.{slug}", "type": "entity", "subtype": subtype,
          "canonical_name": canonical, "aliases": aliases or [], "summary": f"{canonical}."}
    path.write_text(dump_markdown(fm, f"# {canonical}\n"), encoding="utf-8")
    return path


def test_an_unrelated_surname_does_not_block_a_real_match(tmp_path):
    target = _seed(tmp_path, "people", "dana-reed", "Dana Reed", "person")
    _seed(tmp_path, "people", "bob-jones", "Bob Jones", "person")
    index = _load_entity_index(tmp_path)
    found = _match_existing_entity(tmp_path, "Dana Marie Reed", "person", index, source_text="Dana Marie Reed called")
    assert found == target


def test_the_conflicting_candidate_itself_is_still_excluded(tmp_path):
    _seed(tmp_path, "people", "dana-hale", "Dana Hale", "person")
    index = _load_entity_index(tmp_path)
    assert _match_existing_entity(tmp_path, "Dana Reed", "person", index) is None


def test_a_shared_token_does_not_fold_a_place_into_a_person(tmp_path):
    _seed(tmp_path, "people", "atlas-reed", "Atlas Reed", "person")
    index = _load_entity_index(tmp_path)
    assert _match_existing_entity(tmp_path, "Atlas", "place", index) is None
