"""References must name records the graph can actually follow.

`SPEC.md` declares ``links(target_id) REFERENCES files(id)`` and the indexer
inserts each reference verbatim as ``target_id``, so only an exact id produces
a traversable edge. A production vault had **1473 of 2860 edges dead**, 1349 of
them path-shaped — and every one *passed* validation, because the old check
asked whether the file existed rather than whether the reference resolved. The
validator was simultaneously loud about 1298 references that were fine
(archived targets, which keep their ids by design) and silent about half the
graph being disconnected.

Pinned here:

1. Only an id is traversable; a path or filename is repairable, not valid.
2. An archived target resolves — archiving preserves ids on purpose.
3. A draft target is legitimate provenance, neither an error nor repairable.
4. Prose in a reference field is its own defect, reported as such.
5. An ambiguous filename is never auto-resolved.
6. The migration rewrites references and deletes nothing.
"""
from __future__ import annotations

import json

import pytest

from lisan.frontmatter import load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.migrate_refs import migrate_references
from lisan.tools.record_refs import (
    build_reference_index,
    canonicalize_references,
    is_indexable,
    looks_like_prose,
    resolve_reference,
)
from lisan.tools.record_factory import new_entity, new_open_loop
from lisan.tools.validator import validate_vault


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    ensure_vault_layout(v)
    return v


def _write(path, frontmatter, body="# x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(path, frontmatter, body)


def _record(record_id, record_type="knowledge", **extra):
    base = {
        "id": record_id,
        "type": record_type,
        "created": "2026-07-29",
        "updated": "2026-07-29",
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "summary": "s",
        "links": [],
        "confidence": "low",
        "confidence_basis": "b",
        "last_confirmed": "2026-07-29",
        "review_after": "2026-07-29",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. Only an id traverses

def test_id_resolves_path_and_filename_are_repairable_not_valid(vault):
    created = new_entity(vault, "Ruth Example", subtype="person")
    entity_id = load_markdown(created.path).frontmatter["id"]
    rel = created.path.relative_to(vault).as_posix()
    index = build_reference_index(vault)

    exact = resolve_reference(entity_id, vault, index)
    assert exact.ok and exact.kind == "id"

    as_path = resolve_reference(rel, vault, index)
    assert not as_path.ok, "a path is not a joinable target_id — SPEC says links reference files(id)"
    assert as_path.repairable and as_path.target == entity_id

    as_filename = resolve_reference(created.path.name, vault, index)
    assert not as_filename.ok
    assert as_filename.repairable and as_filename.target == entity_id


def test_canonicalize_collapses_every_spelling_to_one_id(vault):
    created = new_entity(vault, "Ruth Example", subtype="person")
    entity_id = load_markdown(created.path).frontmatter["id"]
    rel = created.path.relative_to(vault).as_posix()
    kept, dropped = canonicalize_references([entity_id, rel, created.path.name], vault)
    assert kept == [entity_id], "three spellings of one record are one reference"
    assert dropped == []


# ---------------------------------------------------------------------------
# 2. Archived targets resolve

def test_archived_target_resolves(vault):
    """Epoch archiving and entity merges preserve the id deliberately, and
    archived records stay in files/FTS — so a reference to one is not broken.
    Treating archive as absent produced 695 false errors."""
    _write(vault / "archive" / "entities" / "merged-old.md", _record("entity.merged-old", "entity",
                                                                    subtype="person", canonical_name="Old"))
    index = build_reference_index(vault)
    assert resolve_reference("entity.merged-old", vault, index).ok


def test_archived_target_passes_the_validator(vault):
    _write(vault / "archive" / "entities" / "merged-old.md", _record("entity.merged-old", "entity",
                                                                    subtype="person", canonical_name="Old"))
    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=["entity.merged-old"]))
    messages = [i.message for i in validate_vault(vault).issues]
    assert not any("entity.merged-old" in m for m in messages), messages


# ---------------------------------------------------------------------------
# 3. Draft provenance is legitimate

def test_draft_reference_is_neither_error_nor_repairable(vault):
    """Promotion records the draft a record came from. That is provenance, not
    a graph edge — drafts hold no row in files, so rewriting the reference to a
    draft id would swap one non-joining string for another."""
    _write(vault / "drafts" / "d1.md", _record("draft.d1", "report"))
    _write(vault / "episodes" / "e.md", _record("episode.e", "episode",
                                                links=["drafts/d1.md"], entities=[], evidence=[],
                                                claims=[], source="extraction"))
    index = build_reference_index(vault)
    resolution = resolve_reference("drafts/d1.md", vault, index)
    assert resolution.kind == "unindexed"
    assert not resolution.repairable

    messages = [i.message for i in validate_vault(vault).issues]
    assert not any("drafts/d1.md" in m for m in messages), messages
    assert not is_indexable(vault.joinpath("drafts/d1.md").relative_to(vault))


def test_migration_leaves_draft_references_alone(vault):
    _write(vault / "drafts" / "d1.md", _record("draft.d1", "report"))
    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=["drafts/d1.md"]))
    result = migrate_references(vault, dry_run=False)
    assert result.references_rewritten == 0
    assert load_markdown(vault / "knowledge" / "k.md").frontmatter["links"] == ["drafts/d1.md"]


# ---------------------------------------------------------------------------
# 4 & 5. Prose, and ambiguity

@pytest.mark.parametrize(
    "text",
    [
        "Greenhouse irrigation was replaced on the second of the month.",
        "- result_summary: Counterexample search performed.",
        "Allowing pork to come to room temperature before cooking results in it cooking more evenly.",
    ],
)
def test_prose_is_recognized_as_prose(text, vault):
    index = build_reference_index(vault)
    assert looks_like_prose(text)
    assert resolve_reference(text, vault, index).kind == "prose"


def test_prose_gets_its_own_validator_message(vault):
    _write(vault / "knowledge" / "k.md",
           _record("knowledge.k", links=["Greenhouse irrigation was replaced this month."]))
    messages = [i.message for i in validate_vault(vault).issues]
    assert any("is prose, not a record reference" in m for m in messages), messages
    assert not any("target does not exist" in m for m in messages)


def test_id_shaped_reference_to_nothing_is_still_an_error(vault):
    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=["entity.nobody"]))
    messages = [i.message for i in validate_vault(vault).issues]
    assert any("target does not exist: entity.nobody" in m for m in messages), messages


def test_ambiguous_filename_is_never_auto_resolved(vault):
    """Two records can share a filename across directories. Guessing between
    them is how a wrong reference becomes a confident one."""
    _write(vault / "knowledge" / "shared.md", _record("knowledge.one"))
    _write(vault / "reports" / "shared.md", _record("report.two", "report"))
    index = build_reference_index(vault)
    resolution = resolve_reference("shared.md", vault, index)
    assert not resolution.repairable
    assert "ambiguous" in resolution.detail


# ---------------------------------------------------------------------------
# 6. The migration repairs and never deletes

def test_migration_rewrites_paths_to_ids(vault):
    created = new_entity(vault, "Ruth Example", subtype="person")
    entity_id = load_markdown(created.path).frontmatter["id"]
    rel = created.path.relative_to(vault).as_posix()
    loop = new_open_loop(vault, "A loop", links=[rel])

    dry = migrate_references(vault, dry_run=True)
    assert dry.references_rewritten == 1
    assert load_markdown(loop.path).frontmatter["links"] == [rel], "dry run must not write"

    applied = migrate_references(vault, dry_run=False)
    assert applied.references_rewritten == 1
    assert load_markdown(loop.path).frontmatter["links"] == [entity_id]

    # Idempotent: a second pass finds nothing left to do.
    assert migrate_references(vault, dry_run=False).references_rewritten == 0


def test_migration_keeps_unresolvable_and_prose_entries(vault):
    """Design principle 4: never lose data in the name of tidiness. An
    unresolvable reference is evidence something was once believed to exist."""
    created = new_entity(vault, "Ruth Example", subtype="person")
    rel = created.path.relative_to(vault).as_posix()
    prose = "Greenhouse irrigation was replaced this month."
    loop = new_open_loop(vault, "A loop", links=[rel, "entity.nobody", prose])

    migrate_references(vault, dry_run=False)
    links = load_markdown(loop.path).frontmatter["links"]
    entity_id = load_markdown(created.path).frontmatter["id"]
    assert entity_id in links, "the resolvable one was repaired"
    assert "entity.nobody" in links, "a dangling reference is kept, not deleted"
    assert prose in links, "prose is kept for the owner to decide about"


def test_migration_does_not_touch_untouched_fields(vault):
    """A file-scoped changed flag would re-serialize and de-duplicate later
    fields once any earlier one changed."""
    created = new_entity(vault, "Ruth Example", subtype="person")
    rel = created.path.relative_to(vault).as_posix()
    path = vault / "patterns" / "p.md"
    _write(path, _record("pattern.p", "pattern", links=[rel],
                         supporting_records=["dup", "dup", "entity.nobody"]))
    migrate_references(vault, dry_run=False)
    fm = load_markdown(path).frontmatter
    assert fm["links"] == [load_markdown(created.path).frontmatter["id"]]
    assert fm["supporting_records"] == ["dup", "dup", "entity.nobody"], "untouched field preserved verbatim"


# ---------------------------------------------------------------------------
# 7. The analyst cannot re-introduce the defect

def test_new_pattern_canonicalizes_model_supplied_references(vault):
    """The analyst's supporting_records comes straight from LLM output. A model
    that saw a path in its context emits a path; the write path must turn that
    into the id, or tomorrow's scan rebuilds the disconnected graph."""
    from lisan.tools.record_factory import new_pattern

    created_entity = new_entity(vault, "Ruth Example", subtype="person")
    entity_id = load_markdown(created_entity.path).frontmatter["id"]
    rel = created_entity.path.relative_to(vault).as_posix()
    episode = vault / "episodes" / "2026-07-29-something-happened.md"
    _write(episode, _record("episode.something-happened", "episode", entities=[], evidence=[],
                            claims=[], source="extraction"))

    pattern = new_pattern(
        vault=vault,
        pattern_type="work_loop",
        hypothesis="Work concerns recur across records",
        # exactly the three shapes the analyst produces in the wild
        supporting_records=[rel, "2026-07-29-something-happened.md", "entity.nobody"],
    )
    fm = load_markdown(pattern.path).frontmatter
    assert entity_id in fm["supporting_records"], "path resolved to its id"
    assert "episode.something-happened" in fm["supporting_records"], "filename resolved to its id"
    assert "entity.nobody" not in fm["supporting_records"], "unresolvable reference not written"
    assert fm["links"] == fm["supporting_records"]


# ---------------------------------------------------------------------------
# 8. Provenance to a real-but-unindexed file is not a broken reference

def test_transcript_reference_is_provenance_not_an_error(vault):
    """Evidence cites the transcript it was extracted from. The transcript is a
    real file the indexer makes no node of, so the citation is provenance —
    treating it as dangling reported ~150 correct citations as errors."""
    transcript = vault / "transcripts" / "2026-07-29.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("# transcript\n\nsome turns\n", encoding="utf-8")
    _write(vault / "evidence" / "records" / "e.md",
           _record("evidence.e", "evidence", links=["transcripts/2026-07-29.md"],
                   source_type="chat", actors=[], sensitivity="low", reliability="medium",
                   observed_facts=[], linked_claims=[], linked_episodes=[]))
    index = build_reference_index(vault)
    assert resolve_reference("transcripts/2026-07-29.md", vault, index).kind == "unindexed"
    messages = [i.message for i in validate_vault(vault).issues]
    assert not any("transcripts/2026-07-29.md" in m for m in messages), messages


def test_draft_source_references_are_not_validated(vault):
    """A draft may reference a record that does not exist yet — that is what
    makes it a draft. Holding drafts to committed-bookkeeping standards
    reported 115 non-problems."""
    _write(vault / "drafts" / "d.md", _record("draft.d", "report", links=["entity.does-not-exist-yet"]))
    messages = [i.message for i in validate_vault(vault).issues]
    assert not any("entity.does-not-exist-yet" in m for m in messages), messages


# ---------------------------------------------------------------------------
# 9. The regression guard: a path-form reference must not pass silently

def test_validator_reports_a_path_form_reference(vault):
    """The guard against reverting to `if (vault / link).exists(): continue`.

    That check asked whether the file existed rather than whether the reference
    resolved, so 1349 non-joining edges passed validation while the graph sat
    half-disconnected. A path reference must surface — as a warning, since
    nothing is lost and `lisan migrate refs` fixes it mechanically — and it must
    name the id it should become.
    """
    created = new_entity(vault, "Ruth Example", subtype="person")
    entity_id = load_markdown(created.path).frontmatter["id"]
    rel = created.path.relative_to(vault).as_posix()
    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=[rel]))

    issues = validate_vault(vault).issues
    hits = [i for i in issues if rel in i.message]
    assert hits, f"a path-form reference must be reported; got {[i.message for i in issues]}"
    assert hits[0].severity == "warning", "repairable, so not an error"
    assert "will not traverse" in hits[0].message
    assert entity_id in hits[0].message, "the message must name the id it should become"
    assert "lisan migrate refs" in hits[0].message, "and the command that does it"


def test_bare_filename_reference_is_also_reported(vault):
    created = new_entity(vault, "Ruth Example", subtype="person")
    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=[created.path.name]))
    hits = [i for i in validate_vault(vault).issues if created.path.name in i.message]
    assert hits and hits[0].severity == "warning"
    assert "will not traverse" in hits[0].message


# ---------------------------------------------------------------------------
# 10. A merged-away reference follows the forwarding address

def test_reference_to_a_merged_entity_resolves_to_the_survivor(vault):
    """A merge preserves the fragment's id in the archive, so the reference
    still resolves and nothing looks broken — while retrieval reaches a stub
    instead of the person the fragment became."""
    _write(vault / "archive" / "entities" / "merged-frag.md",
           _record("entity.frag", "entity", subtype="person", canonical_name="Frag",
                   status="archived", merged_into="entity.survivor"))
    _write(vault / "entities" / "people" / "survivor.md",
           _record("entity.survivor", "entity", subtype="person", canonical_name="Survivor"))
    index = build_reference_index(vault)
    resolution = resolve_reference("entity.frag", vault, index)
    assert not resolution.ok, "resolving is not the same as resolving correctly"
    assert resolution.repairable and resolution.target == "entity.survivor"

    _write(vault / "knowledge" / "k.md", _record("knowledge.k", links=["entity.frag"]))
    hits = [i for i in validate_vault(vault).issues if "entity.frag" in i.message]
    assert hits and hits[0].severity == "warning"
    assert "entity.survivor" in hits[0].message


def test_merge_forwarding_survives_a_chain_and_refuses_a_cycle(vault):
    for stem, rid, into in [("a", "entity.a", "entity.b"), ("b", "entity.b", "entity.c")]:
        _write(vault / "archive" / "entities" / f"merged-{stem}.md",
               _record(rid, "entity", subtype="person", canonical_name=stem,
                       status="archived", merged_into=into))
    _write(vault / "entities" / "people" / "c.md",
           _record("entity.c", "entity", subtype="person", canonical_name="C"))
    index = build_reference_index(vault)
    assert index.survivor_of("entity.a") == "entity.c", "follow the whole chain"

    # A cycle must not hang or invent an answer.
    _write(vault / "archive" / "entities" / "merged-x.md",
           _record("entity.x", "entity", subtype="person", canonical_name="X",
                   status="archived", merged_into="entity.y"))
    _write(vault / "archive" / "entities" / "merged-y.md",
           _record("entity.y", "entity", subtype="person", canonical_name="Y",
                   status="archived", merged_into="entity.x"))
    cyclic = build_reference_index(vault)
    assert cyclic.survivor_of("entity.x") is None


def test_migration_rewrites_merged_references(vault):
    _write(vault / "archive" / "entities" / "merged-frag.md",
           _record("entity.frag", "entity", subtype="person", canonical_name="Frag",
                   status="archived", merged_into="entity.survivor"))
    _write(vault / "entities" / "people" / "survivor.md",
           _record("entity.survivor", "entity", subtype="person", canonical_name="Survivor"))
    loop = new_open_loop(vault, "A loop", links=["entity.frag"])
    migrate_references(vault, dry_run=False)
    assert load_markdown(loop.path).frontmatter["links"] == ["entity.survivor"]
