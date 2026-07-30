"""Retire superseded copies of a record that kept its id and changed its name.

The analyst re-derives its patterns on every scan and writes a *new*
date-prefixed file each time, reusing the record id. Over two weeks that made
13 files per pattern — 130 files for 10 patterns — each a near-superset of the
one before.

Nothing downstream wanted them. The index is keyed on id with `INSERT OR
REPLACE`, so exactly one of the 13 was ever a row in `files`; the other twelve
were invisible to retrieval and visible only as 120 duplicate-id errors, which
is the worst of both. Meanwhile the newest copy's reference list grew ~26
entries a day, so the same stale support was re-serialized indefinitely.

This keeps the most recent copy live and moves the rest to
``archive/<type>/superseded-<name>``, marked ``status: superseded``. Nothing is
deleted: an older copy can carry a verdict the newer one lost — one observed
pattern went ``disputed`` on its first day and ``skeptic_reviewed`` after — and
that history is worth more than a tidy directory.

The real fix is upstream: a re-derived record should update in place rather
than found a new file under the same id. This retires the backlog that
assumption produced.
"""
from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..frontmatter import FrontmatterError, load_markdown, write_markdown
from ..paths import vault_root
from .common import iter_markdown_files

# Record types that are *re-derived* rather than authored once, so a repeated id
# means "a newer version of the same finding". Deliberately narrow: for an
# authored record a duplicate id is a defect to look at, not to archive.
REDERIVED_DIRECTORIES = ("patterns", "reviews")


@dataclass(slots=True)
class DuplicateIdResult:
    ids_examined: int = 0
    ids_with_duplicates: int = 0
    files_archived: int = 0
    survivors: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "ids_examined": self.ids_examined,
            "ids_with_duplicates": self.ids_with_duplicates,
            "files_archived": self.files_archived,
            "survivors": self.survivors,
            "archived": self.archived[:60],
        }


def _sort_key(entry: tuple[Path, dict]) -> tuple[str, str, str]:
    path, frontmatter = entry
    return (
        str(frontmatter.get("created") or ""),
        str(frontmatter.get("updated") or ""),
        path.name,
    )


def migrate_duplicate_ids(
    vault: Path | None = None,
    *,
    dry_run: bool = True,
) -> DuplicateIdResult:
    vault = vault or vault_root()
    result = DuplicateIdResult(dry_run=dry_run)
    by_id: dict[str, list[tuple[Path, dict]]] = defaultdict(list)

    for path in iter_markdown_files(vault):
        try:
            rel = path.relative_to(vault)
        except ValueError:
            continue
        if not rel.parts or rel.parts[0] not in REDERIVED_DIRECTORIES:
            continue
        try:
            frontmatter = load_markdown(path).frontmatter
        except (FrontmatterError, OSError):
            continue
        record_id = frontmatter.get("id")
        if isinstance(record_id, str) and record_id.strip():
            by_id[record_id.strip()].append((path, frontmatter))

    result.ids_examined = len(by_id)
    for record_id, entries in sorted(by_id.items()):
        if len(entries) < 2:
            continue
        result.ids_with_duplicates += 1
        ordered = sorted(entries, key=_sort_key)
        survivor_path, _ = ordered[-1]
        result.survivors.append(f"{record_id} -> {survivor_path.relative_to(vault)}")
        for path, frontmatter in ordered[:-1]:
            rel = path.relative_to(vault)
            destination = vault / "archive" / rel.parts[0] / f"superseded-{path.name}"
            result.archived.append(str(rel))
            result.files_archived += 1
            if dry_run:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            updated = dict(frontmatter)
            updated["status"] = "superseded"
            updated["superseded_by_path"] = str(survivor_path.relative_to(vault))
            try:
                body = load_markdown(path).body
            except (FrontmatterError, OSError):
                continue
            write_markdown(path, updated, body)
            shutil.move(str(path), str(destination))

    return result
