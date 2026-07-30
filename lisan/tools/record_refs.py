"""Resolving one record's reference to another. Deterministic, one home.

`links`, `supporting_records`, and friends hold references between records.
Three things were quietly wrong with how they were produced and checked, and
they accounted for 1298 of a production vault's 1470 validation errors —
enough noise that the validator stopped being read at all, which is the real
cost.

1. **Archiving broke references that were never broken.** Epoch archiving and
   entity merges move a record under ``archive/`` and deliberately *preserve
   its id*. The validator's id index excluded archive — correctly, because
   its other job is auditing duplicate ids, and archived copies legitimately
   share ids with the live record. That exclusion leaked into link checking,
   so every reference to an archived record read as dangling. 695 errors,
   all of them pointing at records that exist.

2. **References came straight from the model.** The analyst's
   ``supporting_records`` was passed through from LLM output with no
   resolution, so a model that had seen ``episodes/2026-07-05-….md`` in its
   context would emit the bare filename, or invent
   ``entities/people/example-person.md`` for an entity actually filed as
   ``entity.example-person-with-surnames``. 561 errors. Deterministic-first says the
   model may *propose* a reference; code decides what it resolves to.

3. **Prose ended up in a reference list.** 42 entries were whole sentences
   ("Greenhouse irrigation was replaced on the second of the month."). Reported with the same
   message as a genuine dangling reference, which buried the difference
   between "this target moved" and "this field contains the wrong kind of
   thing entirely".

:func:`resolve_reference` is the shared answer to "what does this string
point at?", used by the validator to judge and by the record factory to
canonicalize before writing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, load_markdown
from .common import iter_markdown_files

# A reference is an id or a path. Anything with sentence punctuation, spaces
# beyond a couple, or sheer length is prose that leaked into the field — a
# different defect from a target that moved, and worth a different message.
_MAX_REFERENCE_LENGTH = 200


# Directories whose records the indexer turns into rows in `files`, and
# therefore into possible graph nodes. Mirrors the validator's structured-record
# rule. `archive/` belongs here: archived snapshots stay in files and FTS by
# design (rebuild_index.py: "Archived entity snapshots stay searchable"), which
# is exactly why a reference to an archived id still resolves.
INDEXED_DIRECTORIES = frozenset({
    "entities", "episodes", "knowledge", "evidence", "state", "decisions",
    "open_loops", "claims", "patterns", "reviews", "reports", "contradictions",
    "archive", "self", "schedules", "confirmations",
})


def is_indexable(rel: Path) -> bool:
    """Can a record at this vault-relative path become a graph node?

    `drafts/` cannot: the indexer skips them, so they hold no row in `files`
    and no reference to one will ever join. Rewriting a draft reference into a
    draft id would swap one non-joining string for another — churn dressed as
    a repair, which a first dry run of this migration proposed across 286 files
    before this check existed.
    """
    return bool(rel.parts) and rel.parts[0] in INDEXED_DIRECTORIES


@dataclass(slots=True)
class ReferenceIndex:
    """Everything a reference could legitimately name, built once per pass."""

    ids: dict[str, Path] = field(default_factory=dict)
    """Indexable record ids, live and archived — the set that can be a graph
    node. Archiving preserves ids by design, so archived targets belong here."""

    live_ids: dict[str, Path] = field(default_factory=dict)
    """Live records only. The duplicate-id audit needs this narrower view."""

    by_basename: dict[str, list[str]] = field(default_factory=dict)
    """Filename -> record ids, for repairing model-shortened paths. A name
    matching more than one record is ambiguous and never auto-resolved."""

    unindexed_paths: dict[str, str] = field(default_factory=dict)
    """Vault-relative path -> id, for records that exist but are not graph
    nodes (drafts). Kept so a reference to one gets an accurate message rather
    than "no such record"."""

    merged_into: dict[str, str] = field(default_factory=dict)
    """Retired id -> surviving id, from an archived record's ``merged_into``.

    A merge preserves the fragment's id in the archive, so a reference to it
    still resolves and nothing looks broken — while retrieval reaches a stub
    instead of the person the fragment became. Following the forwarding
    address is the difference between a reference that works and one that
    merely validates."""

    by_claim_text: dict[str, list[str]] = field(default_factory=dict)
    """Exact ``claim_text`` -> claim ids.

    The writer put claim *text* where claim *ids* belong, in `links` and
    `linked_claims` alike, so 39 references were whole sentences. They are not
    junk: each is the verbatim text of a claim record that exists, which makes
    them repairable by exact match rather than by guesswork. Only exact, only
    unique — a near-match between two claims is a judgement, not a lookup."""

    by_entity_alias: dict[str, list[str]] = field(default_factory=dict)
    """Slugified entity name/alias -> entity ids.

    The last resort for a reference the model invented rather than copied:
    ``entities/people/rosa.md`` names a real person filed under her full name.
    Aliases are the vault's own record of what she is called, so matching
    through them is reading the data rather than guessing at it — and the same
    ambiguity rule applies, because three people sharing a first name make it
    unresolvable, not a coin flip."""

    _path_to_id: dict[str, str] = field(default_factory=dict)

    def id_for_path(self, rel: str) -> str | None:
        return self._path_to_id.get(rel)

    def survivor_of(self, record_id: str) -> str | None:
        """Follow merge forwarding to the final survivor, cycle-safe."""
        seen: set[str] = set()
        current = record_id
        while current in self.merged_into:
            if current in seen:
                return None
            seen.add(current)
            nxt = self.merged_into[current]
            if not nxt or nxt == current:
                return None
            current = nxt
        return current if current != record_id else None


def build_reference_index(vault: Path) -> ReferenceIndex:
    index = ReferenceIndex()
    for path in iter_markdown_files(vault):
        try:
            rel = path.relative_to(vault)
        except ValueError:
            continue
        try:
            frontmatter = load_markdown(path).frontmatter
        except (FrontmatterError, OSError):
            continue
        record_id = frontmatter.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            continue
        record_id = record_id.strip()
        rel_str = rel.as_posix()
        if not is_indexable(rel):
            index.unindexed_paths.setdefault(rel_str, record_id)
            continue
        # A live record outranks an archived one carrying the same id. Archived
        # copies legitimately share ids (epoch snapshots, merges, superseded
        # revisions), and `setdefault` alone would let whichever the directory
        # walk reached first win — so a reference could resolve to a retired
        # snapshot while the current record sat right there.
        archived = "archive" in rel.parts
        if record_id not in index.ids or (not archived and "archive" in index.ids[record_id].parts):
            index.ids[record_id] = rel
        index._path_to_id.setdefault(rel_str, record_id)
        forwarding = frontmatter.get("merged_into")
        if isinstance(forwarding, str) and forwarding.strip() and forwarding.strip() != record_id:
            index.merged_into.setdefault(record_id, forwarding.strip())
        if "archive" not in rel.parts:
            index.live_ids.setdefault(record_id, rel)
            index.by_basename.setdefault(path.name, []).append(record_id)
            claim_text = frontmatter.get("claim_text")
            if isinstance(claim_text, str) and claim_text.strip():
                key = " ".join(claim_text.split())
                index.by_claim_text.setdefault(key, []).append(record_id)
            if str(frontmatter.get("type")) == "entity":
                names = [frontmatter.get("canonical_name"), frontmatter.get("nickname")]
                names.extend(frontmatter.get("aliases") or [])
                for name in names:
                    key = _alias_key(name)
                    if key and record_id not in index.by_entity_alias.setdefault(key, []):
                        index.by_entity_alias[key].append(record_id)
    return index


def _alias_key(value: Any) -> str:
    """Comparison form for a name: lowercased, non-alphanumerics to hyphens.

    Makes ``Rosa Delgado``, ``rosa-delgado`` and ``Rosa_Delgado`` one key,
    which is what lets a reference written as a filename find the person.
    """
    if not isinstance(value, str):
        return ""
    import re as _re

    return _re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def looks_like_prose(value: str) -> bool:
    """True when a reference slot holds a sentence rather than a reference.

    Deliberately conservative: a real id or path has no spaces at all, so a
    single space is already suspicious, but ids are generated from slugified
    summaries and can be long, so length alone is not the test.
    """
    text = value.strip()
    if not text:
        return False
    if len(text) > _MAX_REFERENCE_LENGTH:
        return True
    if text.count(" ") >= 3:
        return True
    # Sentence-enders and list bullets never appear in an id or a path.
    return text.endswith(".") and " " in text or text.startswith("- ")


@dataclass(slots=True)
class Resolution:
    kind: str          # "id" | "path" | "basename" | "prose" | "unknown"
    target: str = ""   # the canonical id when resolvable
    detail: str = ""   # for messages the owner reads

    @property
    def ok(self) -> bool:
        """Traversable **as written**.

        Only an exact id qualifies, and this is not pedantry. SPEC.md declares
        ``links(target_id) REFERENCES files(id)``, and the indexer inserts the
        raw reference string as ``target_id``, so graph retrieval joins it
        against ``files.id``. A path or filename therefore produces an edge
        that matches nothing: on the vault that prompted this work, 1349 of
        2860 edges were dead that way, and every one of them *passed*
        validation because the old check asked whether the file existed rather
        than whether the reference resolved. An instrument that answers a
        different question than the one that matters is worse than silence.
        """
        return self.kind == "id"

    @property
    def repairable(self) -> bool:
        """Names a real record, but not in a form the graph can follow.
        ``lisan migrate refs`` rewrites these to :attr:`target`."""
        return self.kind in {"path", "basename", "merged", "alias", "claim_text"} and bool(self.target)


def resolve_reference(value: Any, vault: Path, index: ReferenceIndex) -> Resolution:
    """What does *value* point at?

    Resolution order — exact id, then vault-relative path, then a unique
    basename match. The path and basename steps do not bless the reference;
    they identify what it *meant*, so the migration can rewrite it to an id.
    A basename matching more than one record stays unresolved, because
    guessing between two records is how a wrong reference becomes a confident
    one.
    """
    if not isinstance(value, str):
        return Resolution("unknown", detail=f"{type(value).__name__} is not a reference")
    text = value.strip()
    if not text:
        return Resolution("unknown", detail="empty reference")
    if text in index.ids:
        survivor = index.survivor_of(text)
        if survivor and survivor in index.ids:
            # Resolvable, but pointing at a fragment that was merged into
            # someone else. Repairable rather than fine: the reference works
            # and still reaches the wrong record.
            return Resolution(
                "merged",
                target=survivor,
                detail=f"{text} was merged into {survivor}",
            )
        return Resolution("id", target=text)
    if text in index.unindexed_paths:
        # A real file, but not a graph node — a draft. Rewriting it to its id
        # would not make the edge join, so it is left exactly as written.
        return Resolution(
            "unindexed",
            detail="points at a draft, which the indexer does not make a graph node",
        )
    if (vault / text).exists():
        canonical = index.id_for_path(text)
        if canonical:
            return Resolution("path", target=canonical, detail=f"path form of {canonical}")
        # The file is really there, but it is not a record the indexer makes a
        # node of — a transcript, a primer document. Evidence citing the
        # transcript it was extracted from is provenance, and provenance to a
        # real file is not a broken reference. Calling it one turned ~150
        # correct citations into errors on first measurement.
        return Resolution(
            "unindexed",
            detail="points at a real file that is not an indexed record (provenance, not a graph edge)",
        )
    if looks_like_prose(text):
        matches = index.by_claim_text.get(" ".join(text.split())) or []
        if len(matches) == 1:
            return Resolution(
                "claim_text",
                target=matches[0],
                detail=f"verbatim claim_text of {matches[0]}",
            )
        if len(matches) > 1:
            return Resolution(
                "prose",
                detail=f"text matches {len(matches)} claims; resolve by hand",
            )
        return Resolution("prose", detail="prose in a reference field")
    basename = text.rsplit("/", 1)[-1]
    if basename.endswith(".md"):
        matches = index.by_basename.get(basename) or []
        if len(matches) == 1:
            return Resolution("basename", target=matches[0], detail=f"filename of {matches[0]}")
        if len(matches) > 1:
            return Resolution("unknown", detail=f"filename {basename!r} matches {len(matches)} records; ambiguous")
    # Last resort: the reference may name an entity by a name it actually goes
    # by, in a file that never existed — a model writing
    # `entities/people/<firstname>.md` for someone filed under a full name.
    stem = basename[:-3] if basename.endswith(".md") else basename
    alias_matches = index.by_entity_alias.get(_alias_key(stem)) or []
    if len(alias_matches) == 1:
        return Resolution(
            "alias",
            target=alias_matches[0],
            detail=f"names {alias_matches[0]} by one of its aliases",
        )
    if len(alias_matches) > 1:
        return Resolution(
            "unknown",
            detail=f"{stem!r} is an alias of {len(alias_matches)} entities; ambiguous — resolve by hand",
        )
    return Resolution("unknown", detail="no record with this id, path, or filename")


def canonicalize_references(
    values: list[Any] | None,
    vault: Path,
    index: ReferenceIndex | None = None,
) -> tuple[list[str], list[str]]:
    """Rewrite a reference list to canonical ids. Returns (kept, dropped).

    Used at write time so a model's approximate reference becomes an exact one
    before it lands, and unresolvable entries never land at all. The caller
    logs what was dropped — silently discarding a reference the model believed
    in is the same dishonesty as inventing one.
    """
    if not values:
        return [], []
    index = index or build_reference_index(vault)
    kept: list[str] = []
    dropped: list[str] = []
    for value in values:
        resolution = resolve_reference(value, vault, index)
        if resolution.ok or resolution.repairable:
            # Both land as the canonical id: an id stays itself, a path or
            # filename becomes the id it was reaching for. This is the step
            # that makes a model's approximate reference exact before it is
            # written, rather than after it has broken the graph.
            if resolution.target and resolution.target not in kept:
                kept.append(resolution.target)
        else:
            dropped.append(f"{value!r}: {resolution.detail}" if isinstance(value, str) else repr(value))
    return kept, dropped
