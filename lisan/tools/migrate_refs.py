"""Rewrite record references to canonical ids, so the graph can follow them.

`SPEC.md` declares ``links(target_id) REFERENCES files(id)``, and the indexer
inserts each reference string verbatim as ``target_id``. A reference written
as a path or a bare filename therefore produces an edge that joins to nothing.
On the vault that prompted this work, **1473 of 2860 edges were dead**, 1349
of them path-shaped — and all of them passed validation, because the old check
asked whether the file existed rather than whether the reference resolved.

This migration converts `entities/people/example-person.md` and `2026-07-05-….md` into
the ids they were reaching for.

**It deletes nothing.** Design principle 4 is "never lose data in the name of
tidiness", and that applies with particular force here: an unresolvable
reference is evidence that something was once believed to exist, which is
worth more than a clean report. So prose entries and references to records
that are genuinely absent are left exactly where they are, and the validator
keeps naming them until the owner decides. The only edit this makes is
replacing a reference with the canonical id of the record it already named.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..frontmatter import FrontmatterError, load_markdown, write_markdown
from ..paths import vault_root
from .common import iter_markdown_files
from .record_refs import build_reference_index, resolve_reference

# Fields that hold references to other records. Deliberately excludes
# `counterexamples`: the graph treats it as a relation, but the analyst writes
# prose findings there, so "repairing" it would mean guessing at which entries
# were ever meant to be references.
# Exactly the list fields `rebuild_index` turns into rows in `links`, plus
# `supporting_records`, which the schema and the validator treat as references
# even though the indexer does not currently make edges from it.
#
# Deliberately excluded: `entities`, `evidence`, `claims` on episodes (the
# indexer builds no edges from them, so rewriting is cosmetic churn across the
# largest record type), `counterexamples` (the analyst writes prose findings
# there), and anything on a draft.
REFERENCE_FIELDS = (
    "links",
    "supporting_records",
    "linked_evidence",
    "linked_claims",
    "linked_episodes",
    "supporting_evidence",
    "contradicting_evidence",
)


@dataclass(slots=True)
class RefMigrationResult:
    files_scanned: int = 0
    files_changed: int = 0
    references_rewritten: int = 0
    left_alone: dict[str, int] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "files_scanned": self.files_scanned,
            "files_changed": self.files_changed,
            "references_rewritten": self.references_rewritten,
            "left_alone": dict(sorted(self.left_alone.items())),
            "changes": self.changes,
        }


def migrate_references(
    vault: Path | None = None,
    *,
    dry_run: bool = True,
    limit_changes_reported: int = 40,
) -> RefMigrationResult:
    vault = vault or vault_root()
    index = build_reference_index(vault)
    result = RefMigrationResult(dry_run=dry_run)

    for path in iter_markdown_files(vault):
        try:
            rel = path.relative_to(vault)
        except ValueError:
            continue
        # Drafts are pre-approval proposals: a draft may legitimately reference
        # a record that does not exist yet, so rewriting them would be editing
        # a model's suggestion rather than the vault's own bookkeeping.
        if rel.parts and rel.parts[0] == "drafts":
            continue
        try:
            doc = load_markdown(path)
        except (FrontmatterError, OSError):
            continue
        frontmatter = dict(doc.frontmatter)
        changed = False

        for field_name in REFERENCE_FIELDS:
            values = frontmatter.get(field_name)
            if not isinstance(values, list) or not values:
                continue
            rewritten: list[object] = []
            field_changed = False
            for value in values:
                resolution = resolve_reference(value, vault, index)
                if resolution.repairable:
                    rewritten.append(resolution.target)
                    result.references_rewritten += 1
                    field_changed = True
                    changed = True
                    if len(result.changes) < limit_changes_reported:
                        result.changes.append(
                            f"{rel}: {field_name}: {value} -> {resolution.target}"
                        )
                else:
                    # id (already right), prose, or unresolvable — untouched.
                    rewritten.append(value)
                    if not resolution.ok:
                        key = f"{resolution.kind} left in place"
                        result.left_alone[key] = result.left_alone.get(key, 0) + 1
            # Per-field, not per-file: a file-scoped flag would re-serialize and
            # de-duplicate every *later* field once any earlier one changed,
            # editing lists this migration has no business touching.
            if field_changed:
                # De-duplicate only within a field we actually rewrote: two
                # different spellings of one record collapse to one id.
                seen: set[object] = set()
                deduped: list[object] = []
                for value in rewritten:
                    marker = value if isinstance(value, str) else repr(value)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    deduped.append(value)
                frontmatter[field_name] = deduped

        result.files_scanned += 1
        if changed:
            result.files_changed += 1
            if not dry_run:
                write_markdown(path, frontmatter, doc.body)

    return result
