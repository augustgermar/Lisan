from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import json_loads_forgiving as _json_loads, utc_now_iso as _iso_now
from .db import connect as _connect
from ..utils import json_dumps_stable


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json_dumps_stable(value)


INGESTION_BATCHES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_batches (
    id TEXT PRIMARY KEY,
    source_root TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    requested_by TEXT,
    options_json TEXT,
    summary_json TEXT,
    error TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingestion_batches_status
    ON ingestion_batches(status, created_at);

CREATE INDEX IF NOT EXISTS idx_ingestion_batches_source_root
    ON ingestion_batches(source_root, created_at);
"""










def ensure_ingestion_batches_table(conn: sqlite3.Connection) -> None:
    conn.executescript(INGESTION_BATCHES_SCHEMA_SQL)
    _ensure_batch_columns(conn)


def _ensure_batch_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(ingestion_batches)").fetchall()}
    additions = {
        "requested_by": "ALTER TABLE ingestion_batches ADD COLUMN requested_by TEXT",
        "options_json": "ALTER TABLE ingestion_batches ADD COLUMN options_json TEXT",
        "summary_json": "ALTER TABLE ingestion_batches ADD COLUMN summary_json TEXT",
        "error": "ALTER TABLE ingestion_batches ADD COLUMN error TEXT",
        "notes": "ALTER TABLE ingestion_batches ADD COLUMN notes TEXT",
        "started_at": "ALTER TABLE ingestion_batches ADD COLUMN started_at TEXT",
        "finished_at": "ALTER TABLE ingestion_batches ADD COLUMN finished_at TEXT",
    }
    for column, sql in additions.items():
        if column not in existing:
            conn.execute(sql)


def _batch_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    batch = dict(row)
    batch["options"] = _json_loads(batch.get("options_json"))
    batch["summary"] = _json_loads(batch.get("summary_json"))
    return batch


def create_batch(
    source_root: str,
    mode: str,
    options: dict[str, Any] | list[Any] | str | None,
    requested_by: str | None = None,
    *,
    db_path: Path | None = None,
    status: str = "planned",
) -> str:
    conn = _connect(db_path)
    try:
        ensure_ingestion_batches_table(conn)
        now = _iso_now()
        batch_id = f"batch.{now.replace(':', '').replace('-', '').replace('T', '').replace('Z', '')}.{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT INTO ingestion_batches (
                id, source_root, mode, status, created_at, started_at, finished_at, requested_by,
                options_json, summary_json, error, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                source_root,
                mode,
                status,
                now,
                now if status in {"running", "completed", "completed_with_errors", "failed", "canceled", "quarantined"} else None,
                now if status in {"completed", "completed_with_errors", "failed", "canceled", "quarantined"} else None,
                requested_by,
                _json_dumps(options),
                None,
                None,
                None,
            ),
        )
        conn.commit()
        return batch_id
    finally:
        conn.close()


def update_batch_status(
    batch_id: str,
    status: str,
    summary: dict[str, Any] | list[Any] | str | None = None,
    error: str | None = None,
    notes: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_ingestion_batches_table(conn)
        now = _iso_now()
        row = conn.execute("SELECT * FROM ingestion_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        current = dict(row)
        updates: list[str] = ["status = ?"]
        values: list[Any] = [status]
        if current.get("started_at") in (None, "") and status in {"running", "completed", "completed_with_errors", "failed", "canceled", "quarantined"}:
            updates.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in {"completed", "completed_with_errors", "failed", "canceled", "quarantined"}:
            updates.append("finished_at = COALESCE(finished_at, ?)")
            values.append(now)
        if summary is not None:
            updates.append("summary_json = ?")
            values.append(_json_dumps(summary))
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        if notes is not None:
            updates.append("notes = ?")
            values.append(notes)
        conn.execute(
            f"UPDATE ingestion_batches SET {', '.join(updates)} WHERE id = ?",
            (*values, batch_id),
        )
        conn.commit()
        return get_batch(batch_id, db_path=db_path)
    finally:
        conn.close()


def get_batch(batch_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_ingestion_batches_table(conn)
        row = conn.execute("SELECT * FROM ingestion_batches WHERE id = ?", (batch_id,)).fetchone()
        return _batch_row(row)
    finally:
        conn.close()


def list_batches(limit: int = 50, status: str | None = None, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        ensure_ingestion_batches_table(conn)
        if status:
            rows = conn.execute(
                """
                SELECT *
                FROM ingestion_batches
                WHERE status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM ingestion_batches
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [batch for batch in (_batch_row(row) for row in rows) if batch is not None]
    finally:
        conn.close()


def artifacts_for_batch(
    batch_id: str,
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    vault = vault or vault_root()
    artifacts: list[dict[str, Any]] = []
    root = vault / "evidence" / "artifacts"
    if root.exists():
        for path in sorted(root.glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            if str(doc.frontmatter.get("type")) != "artifact":
                continue
            if str(doc.frontmatter.get("batch_id") or "") != batch_id:
                continue
            artifacts.append({**doc.frontmatter, "path": str(path.relative_to(vault))})
    if artifacts:
        return artifacts
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM files
            WHERE type = 'artifact'
              AND (
                batch_id = ?
                OR id IN (
                    SELECT artifact_id
                    FROM ingestion_manifest
                    WHERE batch_id = ?
                )
              )
            ORDER BY path ASC
            """,
            (batch_id, batch_id),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def jobs_for_batch(batch_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE batch_id = ?
            ORDER BY created_at ASC, priority ASC, id ASC
            """,
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def manifest_rows_for_batch(batch_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM ingestion_manifest
            WHERE batch_id = ?
            ORDER BY last_seen DESC, source_path ASC
            """,
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def summarize_batch(
    batch_id: str,
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    batch = get_batch(batch_id, db_path=db_path)
    if batch is None:
        return None
    if vault is None:
        options = batch.get("options") if isinstance(batch.get("options"), dict) else {}
        if isinstance(options, dict):
            vault_value = options.get("vault")
            if vault_value:
                vault = Path(str(vault_value))
    artifacts = artifacts_for_batch(batch_id, vault=vault, db_path=db_path)
    jobs = jobs_for_batch(batch_id, db_path=db_path)
    manifest_rows = manifest_rows_for_batch(batch_id, db_path=db_path)
    skipped = [row for row in manifest_rows if str(row.get("status") or "") == "skipped"]
    failed = [row for row in manifest_rows if str(row.get("status") or "") == "failed"]
    parsed = [row for row in manifest_rows if str(row.get("status") or "") in {"parsed", "evidence_extracted"}]
    return {
        "batch": batch,
        "artifacts": artifacts,
        "jobs": jobs,
        "manifest_rows": manifest_rows,
        "skipped_files": skipped,
        "failed_files": failed,
        "parsed_files": parsed,
        "summary": {
            "artifacts": len([row for row in artifacts if str(row.get("type") or "") == "artifact"]),
            "jobs": len(jobs),
            "manifest_rows": len(manifest_rows),
            "skipped": len(skipped),
            "failed": len(failed),
            "parsed": len(parsed),
        },
    }


def quarantine_batch(
    batch_id: str,
    reason: str,
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    vault = vault or vault_root()
    conn = _connect(db_path)
    try:
        ensure_ingestion_batches_table(conn)
        row = conn.execute("SELECT * FROM ingestion_batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        batch = dict(row)
        notes = "\n".join(
            [item for item in [batch.get("notes"), f"Quarantined: {reason}"] if item]
        )
        update_batch_status(batch_id, "quarantined", error=reason, notes=notes, db_path=db_path)
        conn.execute(
            """
            UPDATE ingestion_manifest
            SET status = 'quarantined',
                error = COALESCE(?, error)
            WHERE batch_id = ?
            """,
            (reason, batch_id),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = 'canceled',
                finished_at = COALESCE(finished_at, ?),
                error = COALESCE(error, ?)
            WHERE batch_id = ? AND status IN ('queued', 'retry_wait')
            """,
            (_iso_now(), reason, batch_id),
        )
        rows = conn.execute(
            """
            SELECT *
            FROM files
            WHERE batch_id = ?
              AND type = 'artifact'
            """,
            (batch_id,),
        ).fetchall()
        artifact_paths: list[str] = []
        for artifact_row in rows:
            artifact_paths.append(str(artifact_row["path"]))
            artifact_path = vault / str(artifact_row["path"])
            if not artifact_path.exists():
                continue
            try:
                doc = load_markdown(artifact_path)
            except Exception:
                continue
            frontmatter = dict(doc.frontmatter)
            frontmatter["ingestion_status"] = "quarantined"
            frontmatter["updated"] = _iso_now()[:10]
            if "status" in frontmatter:
                frontmatter["status"] = "quarantined"
            write_markdown(artifact_path, frontmatter, doc.body)
        conn.execute(
            """
            UPDATE files
            SET ingestion_status = 'quarantined',
                status = 'quarantined'
            WHERE batch_id = ? AND type = 'artifact'
            """,
            (batch_id,),
        )
        conn.commit()
        return summarize_batch(batch_id, db_path=db_path)
    finally:
        conn.close()
