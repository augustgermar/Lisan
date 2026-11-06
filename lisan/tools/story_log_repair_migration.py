"""Operator-only, reversible repair of entity story logs polluted by
pipeline JSON (the defect fixed in d4475a4, 2026-10-29).

Until that fix, rewrite_entity_story stored the first 1,200 characters of
the whole draft body — status, task, and the Listener/Writer JSON — as each
``source_log`` entry, and the owner's actual words (the draft's "Source
Text" section, at the end) were cut off. This migration restores each
polluted entry's text from the best evidence available:

1. **draft** — the draft the entry was cut from still exists: its condensed
   body equals the entry text exactly, so its Source Text section is the
   material the entry should always have held.
2. **writer_summary** — the draft is gone, but the entry itself still
   carries the Writer's one-line summary of the turn; that summary replaces
   the JSON.

Entries are rewritten in place (date, folded and order unchanged); a
recovered entry records ``repaired: "<migration id>/<source>"``. Spilled
logs under ``archive/entities/source-logs/*.jsonl`` are repaired the same
way. Nothing is deleted: the bundle keeps byte-for-byte originals, apply is
hash-guarded, and rollback restores them.

Not registered as a CLI command, job, or self-repair capability.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..frontmatter import dump_markdown, load_markdown
from .entity_story import _SOURCE_TEXT_HEADING, _condense, _literal_attribution

MIGRATION_ID = "20261030-story-log-repair"
POLLUTION_MARKERS = ('"action"', "Skeptic approved", "Skeptic rejected", '"memory_type"')
_WRITER_SUMMARY = re.compile(r'Writer[^{]*\{[^}]*?"summary": "((?:[^"\\]|\\.)*)"')


class MigrationError(RuntimeError):
    pass


def is_polluted(text: str) -> bool:
    return any(marker in text for marker in POLLUTION_MARKERS)


def _draft_index(vault: Path) -> dict[str, str]:
    """condensed draft body -> condensed Source Text."""
    index: dict[str, str] = {}
    for path in sorted((vault / "drafts").glob("*.md")):
        try:
            body = load_markdown(path).body
        except Exception:
            continue
        match = _SOURCE_TEXT_HEADING.search(body)
        if not match:
            continue
        source = _condense(body[match.end():].strip())
        if source:
            index.setdefault(_condense(body), source)
    return index


def recover_text(text: str, drafts: dict[str, str]) -> tuple[str, str] | None:
    """(recovered text, source) for a polluted entry, or None."""
    if text in drafts:
        return drafts[text], "draft"
    match = _WRITER_SUMMARY.search(text)
    if match:
        try:
            summary = json.loads(f'"{match.group(1)}"')
        except ValueError:
            summary = match.group(1)
        summary = _condense(summary)
        if summary:
            return summary, "writer_summary"
    return None


def _repair_entries(entries: list[Any], drafts: dict[str, str], fm: dict[str, Any], stats: dict[str, int]) -> bool:
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "")
        if not is_polluted(text):
            continue
        recovered = recover_text(text, drafts)
        if recovered is None:
            stats["unrecoverable"] += 1
            continue
        new_text, source = recovered
        entry["text"] = new_text
        entry["repaired"] = f"{MIGRATION_ID}/{source}"
        if "attribution" in entry:
            entry["attribution"] = _literal_attribution(new_text, fm)
        stats[source] += 1
        changed = True
    return changed


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_migration(vault: Path, migration_root: Path) -> dict[str, Any]:
    """Compute every repair and write a complete rollback bundle; touch no vault file."""
    if migration_root.exists():
        raise MigrationError(f"migration directory already exists: {migration_root}")
    drafts = _draft_index(vault)
    stats = {"draft": 0, "writer_summary": 0, "unrecoverable": 0}
    files: list[dict[str, Any]] = []
    contents: dict[str, tuple[str, str]] = {}

    entity_root = vault / "entities"
    for path in sorted(entity_root.rglob("*.md")) if entity_root.exists() else []:
        before = path.read_text(encoding="utf-8")
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        log = fm.get("source_log")
        if not isinstance(log, list) or not _repair_entries(log, drafts, fm, stats):
            continue
        after = dump_markdown(fm, doc.body)
        load_markdown_text_check(after)
        contents[str(path.relative_to(vault))] = (before, after)

    spill_root = vault / "archive" / "entities" / "source-logs"
    for path in sorted(spill_root.glob("*.jsonl")) if spill_root.exists() else []:
        before = path.read_text(encoding="utf-8")
        out_lines: list[str] = []
        changed = False
        for line in before.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                out_lines.append(line)
                continue
            if isinstance(entry, dict) and _repair_entries([entry], drafts, {}, stats):
                changed = True
                out_lines.append(json.dumps(entry, ensure_ascii=True, sort_keys=True))
            else:
                out_lines.append(line)
        if changed:
            contents[str(path.relative_to(vault))] = (before, "\n".join(out_lines) + ("\n" if before.endswith("\n") else ""))

    for rel, (before, after) in sorted(contents.items()):
        for side, text in (("before", before), ("after", after)):
            target = migration_root / side / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        files.append({
            "path": rel,
            "mode": stat.S_IMODE((vault / rel).stat().st_mode),
            "before_sha256": _sha(before),
            "after_sha256": _sha(after),
        })
    record = {
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": datetime.now(UTC).isoformat(),
        "vault": str(vault),
        "repaired_entries": stats,
        "files": files,
    }
    _write_json(migration_root / "migration.json", record)
    return record


def load_markdown_text_check(text: str) -> None:
    from ..frontmatter import parse_markdown

    parse_markdown(text)  # raises on a malformed rendering


def apply_prepared_migration(vault: Path, migration_root: Path) -> dict[str, Any]:
    """Hash-guarded: refuses if any file changed since prepare."""
    record = json.loads((migration_root / "migration.json").read_text(encoding="utf-8"))
    if record.get("state") != "prepared":
        raise MigrationError(f"migration state is {record.get('state')!r}, not 'prepared'")
    changed = [f["path"] for f in record["files"]
               if _sha((vault / f["path"]).read_text(encoding="utf-8")) != f["before_sha256"]]
    if changed:
        raise MigrationError(f"files changed since prepare; re-prepare: {changed[:5]}")
    for item in record["files"]:
        after = (migration_root / "after" / item["path"]).read_text(encoding="utf-8")
        if _sha(after) != item["after_sha256"]:
            raise MigrationError(f"bundle corrupted: {item['path']}")
        _atomic_write(vault / item["path"], after, int(item["mode"]))
    record["state"] = "applied"
    record["applied_at"] = datetime.now(UTC).isoformat()
    _write_json(migration_root / "migration.json", record)
    return record


def rollback_migration(vault: Path, migration_root: Path) -> dict[str, Any]:
    record = json.loads((migration_root / "migration.json").read_text(encoding="utf-8"))
    for item in record["files"]:
        current = (vault / item["path"]).read_text(encoding="utf-8")
        if _sha(current) != item["after_sha256"]:
            raise MigrationError(f"{item['path']} changed after apply; refusing to overwrite")
    for item in record["files"]:
        before = (migration_root / "before" / item["path"]).read_text(encoding="utf-8")
        _atomic_write(vault / item["path"], before, int(item["mode"]))
    record["state"] = "rolled_back"
    record["rolled_back_at"] = datetime.now(UTC).isoformat()
    _write_json(migration_root / "migration.json", record)
    return record


def _atomic_write(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n", 0o600)
