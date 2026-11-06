"""Operator-only, reversible backfill of entity relationship edges.

Until 2026-10-29 relationship edges lived only in the SQLite ``links``
table and were erased by the next reindex of the entity or the nightly
rebuild; the live index held none. Edges now live in each source entity's
frontmatter ``relationships`` list. This migration writes an owner-approved
manifest of edges — recovered from surviving drafts' Writer output or stated
explicitly in entity summaries — into that list.

The manifest (kept outside the repository, beside the bundle, because it
names real people) is a JSON list of ``{"source", "type", "target",
"basis"}``; ``"symmetric": true`` also writes the reverse edge. Every id must
resolve to a live entity file or prepare refuses. Existing edges are never
duplicated or removed. Bundle, hash-guarded apply, and rollback follow
story_log_repair_migration.
"""
from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..frontmatter import dump_markdown, load_markdown
from .story_log_repair_migration import (
    MigrationError,
    _sha,
    _write_json,
    apply_prepared_migration,
    rollback_migration,
)

MIGRATION_ID = "20261030-relationship-backfill"

__all__ = ["prepare_migration", "apply_prepared_migration", "rollback_migration", "MigrationError"]


def _entity_paths(vault: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in (vault / "entities").rglob("*.md"):
        if "archive" in path.relative_to(vault).parts:
            continue
        try:
            record_id = str(load_markdown(path).frontmatter.get("id") or "")
        except Exception:
            continue
        if record_id:
            out.setdefault(record_id, path)
    return out


def prepare_migration(vault: Path, edges: list[dict[str, Any]], migration_root: Path) -> dict[str, Any]:
    if migration_root.exists():
        raise MigrationError(f"migration directory already exists: {migration_root}")
    paths = _entity_paths(vault)
    expanded: list[dict[str, str]] = []
    for edge in edges:
        source, target, rel_type = str(edge["source"]), str(edge["target"]), str(edge["type"])
        for record_id in (source, target):
            if record_id not in paths:
                raise MigrationError(f"unknown entity id: {record_id}")
        if source == target:
            raise MigrationError(f"self-edge: {source}")
        basis = str(edge.get("basis") or "")
        expanded.append({"source": source, "target": target, "type": rel_type, "basis": basis})
        if edge.get("symmetric"):
            expanded.append({"source": target, "target": source, "type": rel_type, "basis": basis})

    by_source: dict[str, list[dict[str, str]]] = {}
    for edge in expanded:
        by_source.setdefault(edge["source"], []).append(edge)

    files: list[dict[str, Any]] = []
    added = 0
    for source, new_edges in sorted(by_source.items()):
        path = paths[source]
        before = path.read_text(encoding="utf-8")
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        current = [r for r in (fm.get("relationships") or []) if isinstance(r, dict)]
        changed = False
        for edge in new_edges:
            if any(r.get("target") == edge["target"] and r.get("type") == edge["type"] for r in current):
                continue
            current.append({"target": edge["target"], "type": edge["type"],
                            "added_by": MIGRATION_ID, "basis": edge["basis"]})
            added += 1
            changed = True
        if not changed:
            continue
        fm["relationships"] = current
        after = dump_markdown(fm, doc.body)
        rel = str(path.relative_to(vault))
        for side, text in (("before", before), ("after", after)):
            target = migration_root / side / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        files.append({"path": rel, "mode": stat.S_IMODE(path.stat().st_mode),
                      "before_sha256": _sha(before), "after_sha256": _sha(after)})
    record = {
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": datetime.now(UTC).isoformat(),
        "vault": str(vault),
        "edges_added": added,
        "files": files,
    }
    _write_json(migration_root / "manifest.json", {"edges": edges})
    _write_json(migration_root / "migration.json", record)
    return record

