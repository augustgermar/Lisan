"""Reversible retrieval quarantine for invalid structured records.

The Markdown files remain untouched and the validator keeps reporting their
defects.  Quarantine is an index-side visibility overlay: ordinary retrieval
excludes active rows, while an explicit ``include_quarantined=True`` request
can still inspect them.  This module is operator tooling, not an autonomous
repair surface.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, load_markdown
from .db import connect
from .validator import validate_vault


QUARANTINE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS record_quarantine (
    record_path TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    issue_count INTEGER NOT NULL,
    issue_fingerprints TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    migration_id TEXT NOT NULL,
    quarantined_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
)
"""


def ensure_record_quarantine_table(conn) -> None:
    conn.execute(QUARANTINE_SCHEMA_SQL)


def active_quarantine_keys(conn) -> set[str]:
    """Return both ids and path-qualified keys for visibility checks."""
    try:
        ensure_record_quarantine_table(conn)
        rows = conn.execute(
            "SELECT record_id, record_path FROM record_quarantine WHERE active = 1"
        ).fetchall()
    except Exception:
        # Retrieval fails open only if an older read-only database has no
        # overlay table.  A writable current database gets the table above.
        return set()
    keys: set[str] = set()
    for row in rows:
        record_id = str(row[0] or "").strip()
        record_path = str(row[1] or "").strip()
        if record_id:
            keys.add(record_id)
        if record_path:
            keys.add(f"path:{record_path}")
    return keys


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    vault: Path,
    db_path: Path,
    *,
    migration_id: str,
) -> dict[str, Any]:
    """Describe every error-bearing record without changing retrieval."""
    report = validate_vault(vault, db_path=db_path)
    errors_by_path: dict[Path, list[str]] = defaultdict(list)
    for issue in report.issues:
        if issue.severity == "error":
            errors_by_path[issue.path].append(issue.message)

    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        existing_rows = {
            str(row["record_path"]): dict(row)
            for row in conn.execute("SELECT * FROM record_quarantine").fetchall()
        }
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    for path, messages in sorted(errors_by_path.items(), key=lambda item: str(item[0])):
        try:
            rel = path.relative_to(vault).as_posix()
        except ValueError:
            # Aggregate locations such as the entities directory are warnings,
            # not error-bearing records. Fail closed if that ever changes.
            raise ValueError(f"Cannot quarantine error outside vault: {path}")
        if not path.is_file():
            raise ValueError(f"Cannot quarantine non-file validation target: {path}")
        try:
            doc = load_markdown(path)
        except (FrontmatterError, OSError) as exc:
            raise ValueError(f"Cannot identify invalid record {path}: {exc}") from exc
        record_id = str(doc.frontmatter.get("id", "")).strip()
        record_type = str(doc.frontmatter.get("type", "")).strip()
        if not record_id:
            raise ValueError(f"Invalid record has no id and needs path-only handling: {path}")
        records.append(
            {
                "path": rel,
                "id": record_id,
                "type": record_type,
                "summary": str(doc.frontmatter.get("summary", "")),
                "content_sha256": _sha256(path),
                "issue_count": len(messages),
                "issues": sorted(messages),
                "prior_overlay_row": existing_rows.get(rel),
            }
        )
    return {
        "format": 1,
        "migration_id": migration_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vault": str(vault.resolve()),
        "database": str(db_path.resolve()),
        "validator_summary": report.summary(),
        "record_count": len(records),
        "issue_count": sum(record["issue_count"] for record in records),
        "records": records,
        "applied": False,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def apply_manifest(manifest_path: Path, vault: Path, db_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("applied"):
        raise RuntimeError(f"Quarantine manifest is already applied: {manifest_path}")
    records = list(manifest.get("records") or [])
    for record in records:
        target = vault / str(record["path"])
        if not target.is_file() or _sha256(target) != str(record["content_sha256"]):
            raise RuntimeError(f"Record changed since quarantine dry run: {target}")

    now = datetime.now(timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        for record in records:
            conn.execute(
                """
                INSERT INTO record_quarantine (
                    record_path, record_id, record_type, reason, issue_count,
                    issue_fingerprints, content_sha256, migration_id,
                    quarantined_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(record_path) DO UPDATE SET
                    record_id=excluded.record_id,
                    record_type=excluded.record_type,
                    reason=excluded.reason,
                    issue_count=excluded.issue_count,
                    issue_fingerprints=excluded.issue_fingerprints,
                    content_sha256=excluded.content_sha256,
                    migration_id=excluded.migration_id,
                    quarantined_at=excluded.quarantined_at,
                    active=1
                """,
                (
                    record["path"],
                    record["id"],
                    record["type"],
                    "validator_error",
                    int(record["issue_count"]),
                    json.dumps(record["issues"], ensure_ascii=True),
                    record["content_sha256"],
                    manifest["migration_id"],
                    now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    manifest["applied"] = True
    manifest["applied_at"] = now
    write_manifest(manifest_path, manifest)
    return manifest


def rollback_manifest(manifest_path: Path, db_path: Path) -> dict[str, Any]:
    """Restore the exact overlay state recorded by :func:`build_manifest`."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("applied"):
        raise RuntimeError(f"Quarantine manifest is not applied: {manifest_path}")
    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        for record in manifest.get("records") or []:
            conn.execute("DELETE FROM record_quarantine WHERE record_path = ?", (record["path"],))
            prior = record.get("prior_overlay_row")
            if prior:
                columns = [
                    "record_path", "record_id", "record_type", "reason",
                    "issue_count", "issue_fingerprints", "content_sha256",
                    "migration_id", "quarantined_at", "active",
                ]
                conn.execute(
                    f"INSERT INTO record_quarantine ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(prior[column] for column in columns),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    manifest["applied"] = False
    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(manifest_path, manifest)
    return manifest
