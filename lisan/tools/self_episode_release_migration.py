"""Release the legacy self-repair episode after its schema is corrected."""
from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from .db import connect
from .evidence_repair_migration import (
    BACKUP_PATH,
    BACKUP_SHA256,
    EvidenceMigrationError,
    _overlay_rows,
    _write_bytes,
    _write_json_atomic,
)
from .validator import validate_record_candidate


MIGRATION_ID = "20261001-step3-category6-self-repair-episode"


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
            WHERE active=1 AND record_type='self_episode'
            """
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise EvidenceMigrationError(f"expected one quarantined self episode, found {len(rows)}")
    row = rows[0]
    relative = str(row["record_path"])
    path = vault / relative
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != str(row["content_sha256"]):
        raise EvidenceMigrationError(f"quarantine hash drift: {relative}")
    document = load_markdown(path)
    if document.frontmatter.get("event_kind") != "self_repair":
        raise EvidenceMigrationError("unexpected self-episode classification")
    report = validate_record_candidate(path, document.frontmatter, document.body, vault=vault)
    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    if errors:
        raise EvidenceMigrationError(f"self episode is not clean after schema correction: {errors}")
    _write_bytes(migration_root / "before" / relative, content, 0o600)
    _write_bytes(migration_root / "after" / relative, content, 0o600)
    (migration_root / "applied.diff").write_text("", encoding="utf-8")
    prior = _overlay_rows(db_path, [relative])
    metadata = {
        "format": 1,
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "vault": str(vault.resolve()),
        "database": str(db_path.resolve()),
        "restore_anchor": {"path": BACKUP_PATH, "sha256": BACKUP_SHA256, "predates_migration": True},
        "schema_change": "self_episode.event_kind now includes self_repair",
        "records": [{
            "action": "repair",
            "path": relative,
            "record_id": str(row["record_id"]),
            "mode": stat.S_IMODE(path.stat().st_mode),
            "before_sha256": digest,
            "after_sha256": digest,
        }],
        "prior_quarantine_rows": prior,
        "released": [],
        "failed": [],
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata

