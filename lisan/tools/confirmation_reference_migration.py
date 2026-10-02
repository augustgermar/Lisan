"""Operator-only repair for confirmation task tokens stored as graph links.

The migration is intentionally separate from runtime repair machinery.  It
records exact before/after bytes, validates every successor as an existing
self-repair proposal report, and delegates hash-guarded apply/rollback to the
generic incremental migration runner.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import dump_markdown, load_markdown
from .db import connect
from .evidence_repair_migration import (
    BACKUP_PATH,
    BACKUP_SHA256,
    EvidenceMigrationError,
    _overlay_rows,
    _write_bytes,
    _write_json_atomic,
    apply_prepared_migration,
    rollback_migration,
)
from .record_refs import build_reference_index, resolve_reference
from .validator import validate_record_candidate


MIGRATION_ID = "20261001-step3-category5-confirmation-references"


def prepare(vault: Path, db_path: Path, migration_root: Path) -> dict[str, Any]:
    metadata_path = migration_root / "migration.json"
    if metadata_path.exists():
        raise EvidenceMigrationError(f"migration is already prepared: {metadata_path}")
    migration_root.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT record_path, record_id, content_sha256
            FROM record_quarantine
            WHERE active=1 AND record_type='confirmation'
            ORDER BY record_path
            """
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 3:
        raise EvidenceMigrationError(f"expected 3 quarantined confirmations, found {len(rows)}")

    index = build_reference_index(vault)
    before_root = migration_root / "before"
    after_root = migration_root / "after"
    records: list[dict[str, Any]] = []
    diff: list[str] = []
    for row in rows:
        relative = str(row["record_path"])
        path = vault / relative
        before = path.read_bytes()
        if _sha(before) != str(row["content_sha256"]):
            raise EvidenceMigrationError(f"quarantine hash drift: {relative}")
        document = load_markdown(path)
        frontmatter = json.loads(json.dumps(document.frontmatter))
        task_id = str(frontmatter.get("task_id") or "")
        if not task_id.startswith("self-repair:sr-"):
            raise EvidenceMigrationError(f"unexpected confirmation task id: {relative}: {task_id}")
        successor = f"report.{task_id.split(':', 1)[1]}"
        resolution = resolve_reference(successor, vault, index)
        if not resolution.ok or resolution.target != successor:
            raise EvidenceMigrationError(f"proposal report target is not exact: {successor}: {resolution}")
        if frontmatter.get("links") != [task_id]:
            raise EvidenceMigrationError(f"unexpected legacy links: {relative}: {frontmatter.get('links')}")
        frontmatter["links"] = [successor]
        provenance = list(frontmatter.get("reference_provenance") or [])
        provenance.append(
            {
                "field": "links",
                "original_value": task_id,
                "status": "operational_correlation_id",
                "record_target": successor,
                "evidence": "Exact proposal id encoded by the self-repair task id and present as a report record.",
                "migration": MIGRATION_ID,
            }
        )
        frontmatter["reference_provenance"] = provenance
        frontmatter["reference_repair"] = {
            "migration": MIGRATION_ID,
            "policy": "operational task ids remain task_id metadata; graph links require an existing exact record id",
        }
        rendered = dump_markdown(frontmatter, document.body)
        candidate = validate_record_candidate(path, frontmatter, document.body, vault=vault, reference_index=index)
        errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
        if errors:
            raise EvidenceMigrationError(f"candidate remains invalid: {relative}: {errors}")
        after = rendered.encode("utf-8")
        _write_bytes(before_root / relative, before, 0o600)
        _write_bytes(after_root / relative, after, 0o600)
        diff.extend(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        records.append(
            {
                "action": "repair",
                "path": relative,
                "record_id": str(row["record_id"]),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "before_sha256": _sha(before),
                "after_sha256": _sha(after),
                "task_id": task_id,
                "successor": successor,
            }
        )

    paths = [str(row["path"]) for row in records]
    prior = _overlay_rows(db_path, paths)
    if len(prior) != 3 or any(int(row["active"]) != 1 for row in prior.values()):
        raise EvidenceMigrationError("all three confirmations must be actively quarantined")
    (migration_root / "applied.diff").write_text("".join(diff), encoding="utf-8")
    os.chmod(migration_root / "applied.diff", 0o600)
    metadata = {
        "format": 1,
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": _now(),
        "vault": str(vault.resolve()),
        "database": str(db_path.resolve()),
        "restore_anchor": {"path": BACKUP_PATH, "sha256": BACKUP_SHA256, "predates_migration": True},
        "records": records,
        "prior_quarantine_rows": prior,
        "released": [],
        "failed": [],
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata


def clone_test(vault: Path, db_path: Path, migration_root: Path, clone_root: Path) -> dict[str, Any]:
    clone_vault = clone_root / "vault"
    clone_db = clone_root / "lisan.sqlite"
    clone_bundle = clone_root / "bundle"
    shutil.copytree(vault, clone_vault)
    shutil.copy2(db_path, clone_db)
    shutil.copytree(migration_root, clone_bundle)
    metadata_path = clone_bundle / "migration.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["vault"] = str(clone_vault.resolve())
    metadata["database"] = str(clone_db.resolve())
    _write_json_atomic(metadata_path, metadata)
    applied = apply_prepared_migration(clone_vault, clone_db, clone_bundle)
    rolled_back = rollback_migration(clone_vault, clone_db, clone_bundle)
    restored = all(
        _sha((clone_vault / row["path"]).read_bytes()) == row["before_sha256"]
        for row in rolled_back["records"]
    )
    return {
        "applied": applied.get("validator_after_apply"),
        "released": len(applied.get("released", [])),
        "rolled_back": rolled_back.get("state") == "rolled_back",
        "bytes_restored": restored,
    }


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

