"""Operational telemetry lane and reversible report archival."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect as _db_connect
from ..frontmatter import dump_markdown, load_markdown
from ..paths import sqlite_path, vault_root
from .rebuild_index import ensure_index_schema


ARCHIVE_ROOT = Path("archive/operational-reports")
HEAVY_BUNDLE_CHARS = 100_000


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _report_metadata(vault: Path, row: sqlite3.Row) -> dict[str, Any]:
    path = vault / str(row["path"])
    try:
        fm = load_markdown(path).frontmatter
    except Exception:
        fm = {}
    return {
        "id": str(row["id"]),
        "path": str(row["path"]),
        "task": str(fm.get("task") or "unknown"),
        "summary": str(fm.get("summary") or row["summary"] or ""),
        "bundle_chars": int(fm.get("bundle_chars") or 0),
        "records": int(fm.get("records") or 0),
        "updated": str(fm.get("updated") or row["updated"] or ""),
    }


def _path_metadata(vault: Path, path: Path) -> dict[str, Any] | None:
    try:
        fm = load_markdown(path).frontmatter
    except Exception:
        return None
    record_id = str(fm.get("id") or path.stem)
    return {
        "id": record_id,
        "path": str(path.relative_to(vault)),
        "task": str(fm.get("task") or "unknown"),
        "summary": str(fm.get("summary") or ""),
        "bundle_chars": int(fm.get("bundle_chars") or 0),
        "records": int(fm.get("records") or 0),
        "updated": str(fm.get("updated") or ""),
    }


def migrate_operational_reports(
    vault: Path | None = None,
    db_path: Path | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Classify reports and archive duplicate/heavy historical telemetry."""
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    archive = vault / ARCHIVE_ROOT
    stamp = _now()
    manifest_path = archive / f"migration-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    conn = _db_connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        rows = conn.execute(
            "SELECT * FROM files WHERE type = 'report' AND COALESCE(archived_at, '') = '' ORDER BY updated, id"
        ).fetchall()
        metadata = [_report_metadata(vault, row) for row in rows]
        indexed_paths = {item["path"] for item in metadata}
        for path in sorted((vault / "reports").glob("*.md")):
            relative = str(path.relative_to(vault))
            if relative not in indexed_paths:
                item = _path_metadata(vault, path)
                if item is not None:
                    metadata.append(item)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in metadata:
            groups.setdefault((item["task"], item["summary"]), []).append(item)

        archive_items: list[dict[str, Any]] = []
        for group in groups.values():
            group.sort(key=lambda item: (item["updated"], item["id"]), reverse=True)
            kept_duplicate = False
            for item in group:
                heavy = item["bundle_chars"] >= HEAVY_BUNDLE_CHARS or item["records"] >= 500
                duplicate = kept_duplicate
                if heavy or duplicate:
                    archive_items.append({**item, "reason": "bundle-heavy" if heavy else "duplicate-task-summary"})
                else:
                    kept_duplicate = True

        result = {
            "classified_operational": len(metadata),
            "archive_candidates": len(archive_items),
            "archive_manifest": str(manifest_path),
            "dry_run": dry_run,
            "archived": 0,
            "deduplicated": sum(1 for item in archive_items if item["reason"] == "duplicate-task-summary"),
        }
        if dry_run:
            return result

        archive.mkdir(parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []
        moved: list[tuple[Path, Path]] = []
        try:
            for item in archive_items:
                source = vault / item["path"]
                if not source.is_file():
                    continue
                destination = archive / f"{item['id'].replace('/', '_')}.md"
                doc = load_markdown(source)
                fm = dict(doc.frontmatter)
                fm.update({
                    "memory_lane": "operational",
                    "archived_at": stamp,
                    "archive_reason": item["reason"],
                    "archive_original_path": item["path"],
                })
                destination.write_text(dump_markdown(fm, doc.body), encoding="utf-8")
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                source.unlink()
                moved.append((source, destination))
                manifest.append({**item, "archived_path": str(destination.relative_to(vault)), "archived_at": stamp, "sha256": digest})
                conn.execute(
                    "UPDATE files SET path = ?, memory_lane = 'operational', archived_at = ? WHERE id = ?",
                    (str(destination.relative_to(vault)), stamp, item["id"]),
                )
            conn.execute("UPDATE files SET memory_lane = CASE WHEN type = 'report' THEN 'operational' ELSE 'semantic' END WHERE memory_lane IS NULL")
            manifest_path.write_text(json.dumps({"created_at": stamp, "records": manifest}, indent=2) + "\n", encoding="utf-8")
            conn.commit()
        except Exception:
            conn.rollback()
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    destination.replace(source)
            raise
        result["archived"] = len(manifest)
        return result
    finally:
        conn.close()


def restore_operational_archive(manifest_path: Path, vault: Path | None = None, db_path: Path | None = None) -> int:
    """Restore one migration manifest; intended for rollback/testing."""
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    conn = _db_connect(db_path)
    try:
        restored = 0
        for item in manifest.get("records", []):
            source = vault / str(item["archived_path"])
            destination = vault / str(item["path"])
            if not source.is_file() or destination.exists():
                continue
            doc = load_markdown(source)
            fm = dict(doc.frontmatter)
            for key in ("archived_at", "archive_reason", "archive_original_path"):
                fm.pop(key, None)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(dump_markdown(fm, doc.body), encoding="utf-8")
            source.unlink()
            conn.execute("UPDATE files SET path = ?, archived_at = NULL, memory_lane = 'operational' WHERE id = ?", (str(destination.relative_to(vault)), str(item["id"])))
            restored += 1
        conn.commit()
        return restored
    finally:
        conn.close()
