from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..paths import sqlite_path, vault_root
from .job_policy import (
    DEFAULT_STUCK_TIMEOUT_MINUTES,
    coalesce_key_for_job,
    priority_for_job_type,
    unique_group_for_job,
)
from .ingest_batches import ensure_ingestion_batches_table, get_batch
from ..utils import json_dumps_stable as _json_dumps, json_loads_forgiving as _json_loads, parse_utc_timestamp as _parse_timestamp
from .db import connect as _connect


JOB_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "retry_wait",
}

JOB_TYPES = {
    "ingest.scan_path",
    "ingest.parse_file",
    "ingest.extract_evidence",
    "ingest.reindex_artifact",
    "index.rebuild_record",
    "index.rebuild_all",
    "index.embed_pending",
    "analyst.scan",
    "dreamer.maintenance",
    "manifest.regenerate",
    "pattern.audit",
    "skeptic.review_pattern",
    "writer.extract_turn",
    "entity.rewrite_story",
    "task.reminder",
    "task.prompt",
    "task.run_codex",
    "plan.run",
    "capture.observe",
    "deviation.scan",
}

# Indexing/embedding jobs are deterministic and cheap (no LLM call). These are
# the only jobs the end-of-capture drain runs, so semantic retrieval
# works without a manual `lisan jobs run`. The LLM-heavy maintenance jobs
# (analyst.scan, dreamer.maintenance, pattern.audit, manifest.regenerate) stay
# queued for batch/cron — draining them every turn would put an LLM pass on the
# critical path of every capture.
INDEX_JOB_TYPES = {
    "index.rebuild_record",
    "index.rebuild_all",
    "index.embed_pending",
}

_LONG_RUNNING_MINUTES = 15


JOBS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    batch_id TEXT,
    coalesce_key TEXT,
    unique_group TEXT,
    replaces_job_id TEXT,
    coalesced_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    result_ref TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    scheduled_for TEXT,
    recurrence TEXT,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    worker_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
    ON jobs(status, priority, scheduled_for, created_at);

CREATE INDEX IF NOT EXISTS idx_jobs_type_status
    ON jobs(job_type, status, finished_at);

CREATE INDEX IF NOT EXISTS idx_jobs_coalesce
    ON jobs(job_type, coalesce_key, status);

CREATE INDEX IF NOT EXISTS idx_jobs_batch
    ON jobs(batch_id, status);
"""




def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime | None = None) -> str:
    dt = dt or _now()
    return dt.isoformat().replace("+00:00", "Z")


def _normalize_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return _iso(dt.astimezone(timezone.utc))
    if isinstance(value, date):
        dt = datetime.combine(value, time.min, tzinfo=timezone.utc)
        return _iso(dt)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text or " " in text:
            dt = datetime.fromisoformat(text)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return _iso(dt.astimezone(timezone.utc))
        parsed_date = date.fromisoformat(text)
        return _iso(datetime.combine(parsed_date, time.min, tzinfo=timezone.utc))
    except ValueError:
        return text






def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    job = dict(row)
    job["payload"] = _json_loads(job.get("payload_json"))
    job["result"] = _json_loads(job.get("result_json"))
    return job


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    conn.executescript(JOBS_SCHEMA_SQL)
    ensure_ingestion_batches_table(conn)
    _ensure_jobs_columns(conn)


def _ensure_jobs_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    additions = {
        "batch_id": "ALTER TABLE jobs ADD COLUMN batch_id TEXT",
        "coalesce_key": "ALTER TABLE jobs ADD COLUMN coalesce_key TEXT",
        "unique_group": "ALTER TABLE jobs ADD COLUMN unique_group TEXT",
        "replaces_job_id": "ALTER TABLE jobs ADD COLUMN replaces_job_id TEXT",
        "coalesced_count": "ALTER TABLE jobs ADD COLUMN coalesced_count INTEGER NOT NULL DEFAULT 0",
        "recurrence": "ALTER TABLE jobs ADD COLUMN recurrence TEXT",
    }
    for column, sql in additions.items():
        if column not in existing:
            conn.execute(sql)


def _payload_with_policy(
    payload: dict[str, Any],
    *,
    coalesce_key: str | None,
    unique_group: str | None,
    batch_id: str | None,
) -> dict[str, Any]:
    data = dict(payload)
    if coalesce_key is not None:
        data.setdefault("coalesce_key", coalesce_key)
    if unique_group is not None:
        data.setdefault("unique_group", unique_group)
    if batch_id is not None:
        data.setdefault("batch_id", batch_id)
    return data


def _coalesce_or_insert(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    job_type: str,
    priority: int,
    payload: dict[str, Any] | list[Any] | str,
    max_attempts: int,
    created_at: str,
    scheduled_for: str | None,
    batch_id: str | None,
    coalesce_key: str | None,
    unique_group: str | None,
    replaces_job_id: str | None,
    coalesced_count: int | None,
    recurrence: str | None = None,
) -> str | None:
    if not coalesce_key:
        conn.execute(
            """
            INSERT INTO jobs (
                id, job_type, status, priority, batch_id, coalesce_key, unique_group, replaces_job_id,
                coalesced_count, payload_json, result_json, result_ref, attempts, max_attempts,
                created_at, scheduled_for, recurrence, started_at, finished_at, error, worker_id
            ) VALUES (?, ?, 'queued', ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                job_id,
                job_type,
                priority,
                batch_id,
                unique_group,
                replaces_job_id,
                int(coalesced_count or 0),
                _json_dumps(payload if payload is not None else {}),
                max_attempts,
                created_at,
                scheduled_for,
                recurrence,
            ),
        )
        return None

    same_key_rows = conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE job_type = ? AND coalesce_key = ? AND status IN ('queued', 'running')
        ORDER BY
            CASE status WHEN 'queued' THEN 0 ELSE 1 END,
            priority ASC,
            created_at ASC,
            id ASC
        """,
        (job_type, coalesce_key),
    ).fetchall()

    queued_row = next((row for row in same_key_rows if str(row["status"]) == "queued"), None)
    running_row = next((row for row in same_key_rows if str(row["status"]) == "running"), None)

    if queued_row is not None:
        merged_payload = _merge_payloads(_json_loads(queued_row["payload_json"]), payload)
        merged_priority = min(int(queued_row["priority"] or priority), int(priority))
        merged_max_attempts = max(int(queued_row["max_attempts"] or max_attempts), int(max_attempts))
        merged_scheduled_for = _min_timestamp(str(queued_row["scheduled_for"] or "") or None, scheduled_for)
        existing_count = int(queued_row["coalesced_count"] or 0)
        conn.execute(
            """
            UPDATE jobs
            SET payload_json = ?,
                priority = ?,
                max_attempts = ?,
                scheduled_for = ?,
                batch_id = COALESCE(?, batch_id),
                unique_group = COALESCE(?, unique_group),
                replaces_job_id = COALESCE(?, replaces_job_id),
                recurrence = COALESCE(?, recurrence),
                coalesced_count = coalesced_count + 1
            WHERE id = ?
            """,
            (
                _json_dumps(merged_payload),
                merged_priority,
                merged_max_attempts,
                merged_scheduled_for,
                batch_id,
                unique_group,
                replaces_job_id,
                recurrence,
                queued_row["id"],
            ),
        )
        return str(queued_row["id"])

    if running_row is not None:
        follow_up = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE job_type = ? AND coalesce_key = ? AND status = 'queued' AND replaces_job_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (job_type, coalesce_key, running_row["id"]),
        ).fetchone()
        if follow_up is not None:
            merged_payload = _merge_payloads(_json_loads(follow_up["payload_json"]), payload)
            merged_priority = min(int(follow_up["priority"] or priority), int(priority))
            merged_max_attempts = max(int(follow_up["max_attempts"] or max_attempts), int(max_attempts))
            merged_scheduled_for = _min_timestamp(str(follow_up["scheduled_for"] or "") or None, scheduled_for)
            conn.execute(
                """
                UPDATE jobs
                SET payload_json = ?,
                    priority = ?,
                    max_attempts = ?,
                    scheduled_for = ?,
                    batch_id = COALESCE(?, batch_id),
                    unique_group = COALESCE(?, unique_group),
                    coalesced_count = coalesced_count + 1
                WHERE id = ?
                """,
                (
                    _json_dumps(merged_payload),
                    merged_priority,
                    merged_max_attempts,
                    merged_scheduled_for,
                    batch_id,
                    unique_group,
                    follow_up["id"],
                ),
            )
            return str(follow_up["id"])

        conn.execute(
            """
            INSERT INTO jobs (
                id, job_type, status, priority, batch_id, coalesce_key, unique_group, replaces_job_id,
                coalesced_count, payload_json, result_json, result_ref, attempts, max_attempts,
                created_at, scheduled_for, recurrence, started_at, finished_at, error, worker_id
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
            """,
            (
                job_id,
                job_type,
                priority,
                batch_id,
                coalesce_key,
                unique_group,
                running_row["id"],
                int(coalesced_count or 0),
                _json_dumps(payload if payload is not None else {}),
                max_attempts,
                created_at,
                scheduled_for,
                recurrence,
            ),
        )
        return None

    conn.execute(
        """
        INSERT INTO jobs (
            id, job_type, status, priority, batch_id, coalesce_key, unique_group, replaces_job_id,
            coalesced_count, payload_json, result_json, result_ref, attempts, max_attempts,
            created_at, scheduled_for, recurrence, started_at, finished_at, error, worker_id
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
        """,
        (
            job_id,
            job_type,
            priority,
            batch_id,
            coalesce_key,
            unique_group,
            replaces_job_id,
            int(coalesced_count or 0),
            _json_dumps(payload if payload is not None else {}),
            max_attempts,
            created_at,
            scheduled_for,
            recurrence,
        ),
    )
    return None


def _merge_payloads(existing: Any, incoming: Any) -> Any:
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        existing_batch_ids = _payload_batch_ids(existing)
        incoming_batch_ids = _payload_batch_ids(incoming)
        for key, value in incoming.items():
            if key in {"batch_id", "batch_ids"}:
                continue
            merged[key] = value
        merged_batch_ids = list(dict.fromkeys(existing_batch_ids + incoming_batch_ids))
        if merged_batch_ids:
            merged["batch_ids"] = merged_batch_ids
            merged["batch_id"] = merged_batch_ids[-1]
        return merged
    return incoming


def _payload_batch_ids(payload: dict[str, Any]) -> list[str]:
    batch_ids: list[str] = []
    batch_id = payload.get("batch_id")
    if batch_id:
        batch_ids.append(str(batch_id))
    extra = payload.get("batch_ids")
    if isinstance(extra, list):
        for item in extra:
            if item:
                batch_ids.append(str(item))
    return list(dict.fromkeys(batch_ids))


def _min_timestamp(a: str | None, b: str | None) -> str | None:
    values = [value for value in [a, b] if value]
    if not values:
        return None
    parsed = [value for value in (_parse_timestamp(value) for value in values) if value is not None]
    if not parsed:
        return values[0]
    return _iso(min(parsed))


def enqueue_job(
    job_type: str,
    payload: dict[str, Any] | list[Any] | str | None,
    priority: int | None = None,
    scheduled_for: datetime | date | str | None = None,
    max_attempts: int = 3,
    batch_id: str | None = None,
    coalesce_key: str | None = None,
    unique_group: str | None = None,
    replaces_job_id: str | None = None,
    coalesced_count: int | None = None,
    recurrence: str | None = None,
    db_path: Path | None = None,
) -> str:
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unsupported job_type: {job_type}")
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        payload_obj = payload if payload is not None else {}
        if priority is None:
            priority = priority_for_job_type(job_type)
        if coalesce_key is None and isinstance(payload_obj, dict):
            coalesce_key = coalesce_key_for_job(job_type, payload_obj)
        if unique_group is None and isinstance(payload_obj, dict):
            unique_group = unique_group_for_job(job_type, payload_obj)
        if isinstance(payload_obj, dict):
            payload_obj = _payload_with_policy(payload_obj, coalesce_key=coalesce_key, unique_group=unique_group, batch_id=batch_id)
            if batch_id is None:
                batch_id = str(payload_obj.get("batch_id") or "") or None
        job_id = f"job.{_iso().replace(':', '').replace('-', '').replace('Z', '')}.{uuid.uuid4().hex[:12]}"
        created_at = _iso()
        normalized_payload = payload_obj if isinstance(payload_obj, (dict, list, str)) else {}
        coalesced = _coalesce_or_insert(
            conn,
            job_id=job_id,
            job_type=job_type,
            priority=int(priority),
            payload=normalized_payload,
            max_attempts=int(max_attempts),
            created_at=created_at,
            scheduled_for=_normalize_timestamp(scheduled_for),
            batch_id=batch_id,
            coalesce_key=coalesce_key,
            unique_group=unique_group,
            replaces_job_id=replaces_job_id,
            coalesced_count=coalesced_count,
            recurrence=recurrence,
        )
        if coalesced is not None:
            conn.commit()
            return coalesced
        conn.commit()
        return job_id
    finally:
        conn.close()


def _claimable_clause(now_iso: str) -> str:
    return "(status = 'queued' AND (scheduled_for IS NULL OR scheduled_for <= ?))"


def claim_next_job(
    worker_id: str,
    db_path: Path | None = None,
    job_types: set[str] | None = None,
) -> dict[str, Any] | None:
    """Claim the next queued job. When ``job_types`` is given, only jobs of
    those types are eligible (used by the end-of-capture index drain)."""
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        now_iso = _iso()
        conn.execute("BEGIN IMMEDIATE")
        type_clause = ""
        params: list[Any] = [now_iso]
        if job_types:
            placeholders = ", ".join("?" for _ in job_types)
            type_clause = f" AND jobs.job_type IN ({placeholders})"
            params.extend(sorted(job_types))
        row = conn.execute(
            f"""
            SELECT jobs.*
            FROM jobs
            LEFT JOIN ingestion_batches ON ingestion_batches.id = jobs.batch_id
            WHERE jobs.status = 'queued' AND (jobs.scheduled_for IS NULL OR jobs.scheduled_for <= ?)
              AND COALESCE(ingestion_batches.status, '') != 'quarantined'{type_clause}
            ORDER BY jobs.priority ASC, jobs.scheduled_for ASC, jobs.created_at ASC, jobs.id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE jobs
            SET status = 'running',
                attempts = attempts + 1,
                started_at = ?,
                worker_id = ?,
                error = NULL
            WHERE id = ?
            """,
            (now_iso, worker_id, row["id"]),
        )
        conn.commit()
        return _row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())
    finally:
        conn.close()


def _promote_due_retry_wait_jobs(conn: sqlite3.Connection) -> int:
    now_iso = _iso()
    cursor = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued'
        WHERE status = 'retry_wait'
          AND scheduled_for IS NOT NULL
          AND scheduled_for <= ?
        """,
        (now_iso,),
    )
    return int(cursor.rowcount or 0)


def mark_job_succeeded(
    job_id: str,
    result: Any = None,
    result_ref: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        now_iso = _iso()
        conn.execute(
            """
            UPDATE jobs
            SET status = 'succeeded',
                result_json = ?,
                result_ref = ?,
                finished_at = ?,
                error = NULL
            WHERE id = ?
            """,
            (_json_dumps(result) if result is not None else None, result_ref, now_iso, job_id),
        )
        conn.commit()
        return get_job(job_id, db_path=db_path)
    finally:
        conn.close()


def mark_job_failed(
    job_id: str,
    error: str,
    retry: bool = True,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        now_iso = _iso()
        row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        attempts = int(row["attempts"] or 0)
        max_attempts = int(row["max_attempts"] or 0)
        if retry and attempts < max_attempts:
            status = "retry_wait"
            scheduled_for = now_iso
            finished_at = now_iso
        else:
            status = "failed"
            scheduled_for = None
            finished_at = now_iso
        conn.execute(
            """
            UPDATE jobs
            SET status = ?,
                finished_at = ?,
                error = ?,
                scheduled_for = ?,
                started_at = NULL,
                worker_id = NULL
            WHERE id = ?
            """,
            (status, finished_at, str(error), scheduled_for, job_id),
        )
        conn.commit()
        return get_job(job_id, db_path=db_path)
    finally:
        conn.close()


def list_jobs(status: str | None = None, limit: int = 50, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        if status:
            rows = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE status = ?
                ORDER BY created_at DESC, priority ASC, id ASC
                LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM jobs
                ORDER BY created_at DESC, priority ASC, id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(_row_to_job(row) or {}) for row in rows]
    finally:
        conn.close()


def get_job(job_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row)
    finally:
        conn.close()


def cancel_job(job_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        now_iso = _iso()
        conn.execute(
            """
            UPDATE jobs
            SET status = 'canceled',
                finished_at = ?,
                error = COALESCE(error, 'canceled by user')
            WHERE id = ?
            """,
            (now_iso, job_id),
        )
        conn.commit()
        return get_job(job_id, db_path=db_path)
    finally:
        conn.close()


def retry_job(job_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        now_iso = _iso()
        conn.execute(
            """
            UPDATE jobs
            SET status = 'queued',
                scheduled_for = ?,
                finished_at = NULL,
                error = NULL,
                started_at = NULL,
                worker_id = NULL
            WHERE id = ? AND status IN ('failed', 'retry_wait', 'canceled')
            """,
            (now_iso, job_id),
        )
        conn.commit()
        return get_job(job_id, db_path=db_path)
    finally:
        conn.close()


def _normalize_job_result(result: Any) -> tuple[Any, str | None]:
    if result is None:
        return None, None
    if isinstance(result, Path):
        return {"path": str(result)}, str(result)
    if is_dataclass(result):
        data = asdict(result)
        ref = None
        for key in ("report_path", "path", "artifact_path", "state_path", "draft_path"):
            value = data.get(key)
            if value:
                ref = str(value)
                break
        for key, value in list(data.items()):
            if isinstance(value, Path):
                data[key] = str(value)
            elif isinstance(value, list):
                data[key] = [str(item) if isinstance(item, Path) else item for item in value]
        return data, ref
    if isinstance(result, dict):
        ref = None
        for key in ("report_path", "path", "artifact_path", "state_path", "draft_path"):
            value = result.get(key)
            if value:
                ref = str(value)
                break
        return result, ref
    return result, None


def _resolve_vault(payload: dict[str, Any], fallback: Path | None = None) -> Path:
    value = payload.get("vault") or payload.get("vault_path")
    if value:
        return Path(str(value))
    return fallback or vault_root()


def _resolve_db_path(payload: dict[str, Any], fallback: Path | None = None) -> Path | None:
    value = payload.get("db_path")
    if value:
        return Path(str(value))
    return fallback


def _resolve_batch_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("batch_id")
    if value:
        return str(value)
    return None


def dispatch_job(
    job: dict[str, Any],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> Any:
    payload = job.get("payload") if isinstance(job, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    job_type = str(job.get("job_type") or "").strip()
    vault = _resolve_vault(payload, fallback=vault)
    db_path = _resolve_db_path(payload, fallback=db_path)
    batch_id = _resolve_batch_id(payload)

    if job_type == "index.rebuild_record":
        from .rebuild_index import rebuild_index

        embeddings_file = Path(str(payload.get("embeddings_file"))) if payload.get("embeddings_file") else None
        return rebuild_index(vault=vault, db_path=db_path, embeddings_file=embeddings_file)

    if job_type == "index.rebuild_all":
        from .rebuild_index import rebuild_index

        embeddings_file = Path(str(payload.get("embeddings_file"))) if payload.get("embeddings_file") else None
        return rebuild_index(vault=vault, db_path=db_path, embeddings_file=embeddings_file)

    if job_type == "index.embed_pending":
        from .rebuild_index import embed_pending_records

        embeddings_file = Path(str(payload.get("embeddings_file"))) if payload.get("embeddings_file") else None
        return embed_pending_records(vault=vault, db_path=db_path, embeddings_file=embeddings_file)

    if job_type == "ingest.scan_path":
        from .ingest import scan_path
        from .ingest_batches import create_batch

        scan_target = payload.get("path") or payload.get("source_path") or payload.get("root")
        if not scan_target:
            raise ValueError("ingest.scan_path requires path")
        if not batch_id:
            batch_id = create_batch(str(Path(str(scan_target)).resolve()), "scan", payload, db_path=db_path)
            payload["batch_id"] = batch_id
        return scan_path(Path(str(scan_target)), vault=vault, db_path=db_path, queue_jobs=True, batch_id=batch_id, batch_mode=str(payload.get("mode") or "scan"), batch_options=payload)

    if job_type == "ingest.parse_file":
        from .ingest import parse_file

        artifact_id = str(payload.get("artifact_id") or "").strip()
        source_path = str(payload.get("source_path") or "").strip()
        artifact_hash = str(payload.get("artifact_hash") or "").strip()
        if not artifact_id or not source_path or not artifact_hash:
            raise ValueError("ingest.parse_file requires artifact_id, source_path, and artifact_hash")
        return parse_file(
            artifact_id=artifact_id,
            source_path=source_path,
            artifact_hash=artifact_hash,
            file_name=str(payload.get("file_name") or Path(source_path).name),
            file_ext=str(payload.get("file_ext") or Path(source_path).suffix),
            source_type=str(payload.get("source_type") or "text"),
            imported_at=str(payload.get("imported_at") or _iso()),
            modified_at=str(payload.get("modified_at") or _iso()),
            size_bytes=int(payload.get("size_bytes") or 0),
            sensitivity=str(payload.get("sensitivity") or "medium"),
            source_uri=payload.get("source_uri"),
            db_path=db_path,
            vault=vault,
            extracted_text_ref=payload.get("extracted_text_ref"),
            batch_id=batch_id,
        )

    if job_type == "ingest.extract_evidence":
        from .ingest import extract_evidence

        artifact_id = str(payload.get("artifact_id") or "").strip()
        source_path = str(payload.get("source_path") or "").strip()
        artifact_hash = str(payload.get("artifact_hash") or "").strip()
        if not artifact_id or not source_path or not artifact_hash:
            raise ValueError("ingest.extract_evidence requires artifact_id, source_path, and artifact_hash")
        return extract_evidence(
            artifact_id=artifact_id,
            source_path=source_path,
            artifact_hash=artifact_hash,
            file_name=str(payload.get("file_name") or Path(source_path).name),
            file_ext=str(payload.get("file_ext") or Path(source_path).suffix),
            source_type=str(payload.get("source_type") or "text"),
            imported_at=str(payload.get("imported_at") or _iso()),
            modified_at=str(payload.get("modified_at") or _iso()),
            size_bytes=int(payload.get("size_bytes") or 0),
            sensitivity=str(payload.get("sensitivity") or "medium"),
            extracted_text_ref=payload.get("extracted_text_ref"),
            source_uri=payload.get("source_uri"),
            db_path=db_path,
            vault=vault,
            batch_id=batch_id,
        )

    if job_type == "ingest.reindex_artifact":
        from .ingest import reindex_artifact

        artifact_id = str(payload.get("artifact_id") or "").strip()
        source_path = str(payload.get("source_path") or "").strip()
        artifact_hash = str(payload.get("artifact_hash") or "").strip()
        if not artifact_id or not source_path or not artifact_hash:
            raise ValueError("ingest.reindex_artifact requires artifact_id, source_path, and artifact_hash")
        return reindex_artifact(
            artifact_id=artifact_id,
            source_path=source_path,
            artifact_hash=artifact_hash,
            batch_id=batch_id,
            db_path=db_path,
            vault=vault,
        )

    if job_type == "analyst.scan":
        from .analyst_ops import run_analyst_scan

        return run_analyst_scan(vault=vault, provider=provider, model=model)

    if job_type == "deviation.scan":
        from ..config import load_config
        from .deviations import scan_deviations

        return scan_deviations(vault, db_path=db_path, config=load_config())

    if job_type == "dreamer.maintenance":
        from .dreamer_ops import run_dreamer_task

        task = str(payload.get("task") or "compress")
        return run_dreamer_task(vault=vault, task=task, provider=provider, model=model)

    if job_type == "manifest.regenerate":
        from .manifest_gen import generate_manifests

        manifests = generate_manifests(vault=vault, write=True)
        return {"manifests": sorted(manifests.keys())}

    if job_type == "pattern.audit":
        from .dreamer_ops import audit_patterns, format_pattern_audit

        report = audit_patterns(vault)
        out = vault / "reports" / f"pattern-audit-{_iso().replace(':', '').replace('-', '').replace('Z', '')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(format_pattern_audit(report), encoding="utf-8")
        return {"report_path": str(out), "report": report}

    if job_type == "skeptic.review_pattern":
        from .analyst_ops import review_pattern

        pattern_path = payload.get("pattern_path")
        if not pattern_path and payload.get("pattern_id"):
            pattern_path = _find_pattern_path(vault, str(payload.get("pattern_id")))
        if not pattern_path:
            raise ValueError("skeptic.review_pattern requires pattern_path or pattern_id")
        return review_pattern(
            vault=vault,
            pattern_path=Path(str(pattern_path)),
            provider=provider,
            model=model,
        )

    if job_type == "writer.extract_turn":
        from .capture import capture_text

        text = str(payload.get("text") or "")
        if not text:
            raise ValueError("writer.extract_turn requires text")
        return capture_text(
            vault=vault,
            text=text,
            conversation_id=payload.get("conversation_id"),
            speaker=str(payload.get("speaker") or "USER"),
            provider=provider or payload.get("provider"),
            model=model or payload.get("model"),
            conversation_policy=payload.get("conversation_policy") if isinstance(payload.get("conversation_policy"), dict) else None,
            queue_background=False,
            db_path=db_path,
        )

    if job_type == "capture.observe":
        from .memory_pipeline import run_memory_pipeline

        text = str(payload.get("text") or "")
        if not text:
            raise ValueError("capture.observe requires text")
        result = run_memory_pipeline(
            vault=vault,
            text=text,
            conversation_id=payload.get("conversation_id"),
            provider=provider or payload.get("provider"),
            model=model or payload.get("model"),
            db_path=db_path,
            observed_response=str(payload.get("response") or ""),
            observed_tool_calls=payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else [],
        )
        # Living entity stories: every entity that received new material gets
        # its narrative re-told. This ran inside capture_text on the legacy
        # path; the observer bypasses that, so enqueue the rewrites here — or
        # entity stories never grow past their first stub.
        rewrites = _enqueue_entity_rewrites(
            result, vault=vault, conversation_id=payload.get("conversation_id"), db_path=db_path,
        )
        return {
            "action": result.action,
            "mode": result.mode,
            "draft": str(result.draft_path) if result.draft_path else None,
            "skeptic_approved": result.skeptic_approved,
            "entity_rewrites_queued": rewrites,
        }

    if job_type == "plan.run":
        from .plans import run_plan_step
        from .scheduler import current_send_fn

        return run_plan_step(
            job,
            vault=vault,
            db_path=db_path,
            provider=provider,
            model=model,
            send_fn=current_send_fn(),
        )

    if job_type in {"task.reminder", "task.prompt", "task.run_codex"}:
        from .scheduler import current_send_fn, run_task_job

        return run_task_job(
            job,
            vault=vault,
            db_path=db_path,
            provider=provider,
            model=model,
            send_fn=current_send_fn(),
        )

    if job_type == "entity.rewrite_story":
        from .entity_story import rewrite_entity_story

        entity_path_str = str(payload.get("entity_path") or "").strip()
        if not entity_path_str:
            raise ValueError("entity.rewrite_story requires entity_path")
        entity_path = Path(entity_path_str)
        draft_path_str = str(payload.get("draft_path") or "").strip()
        transcript_path_str = str(payload.get("transcript_path") or "").strip()
        return rewrite_entity_story(
            vault=vault,
            entity_path=entity_path,
            draft_path=Path(draft_path_str) if draft_path_str else None,
            transcript_path=Path(transcript_path_str) if transcript_path_str else None,
            conversation_id=payload.get("conversation_id"),
            provider=provider or payload.get("provider"),
            model=model or payload.get("model"),
            db_path=db_path,
            force_compact=bool(payload.get("force_compact")),
        )

    raise ValueError(f"Unsupported job_type: {job_type}")


def _enqueue_entity_rewrites(result, *, vault: Path, conversation_id, db_path) -> int:
    """Queue an entity.rewrite_story job for each entity this turn advanced.

    Two sources: entities the writer explicitly touched, PLUS existing
    entities named anywhere in the recent conversation thread. The second
    source is essential — a conversation about one person quickly shifts to
    pronouns ("she never married"), and the isolated-turn writer stops naming
    the entity, so without this an ongoing story would freeze the moment the
    user stops repeating the name."""
    from ..frontmatter import load_markdown
    from .job_policy import priority_for_job_type

    touched: dict[str, Path] = {}
    for entity_path in (getattr(result, "entities_touched", None) or []):
        touched[str(entity_path)] = entity_path
    for entity_path in _entities_named_in_conversation(vault, conversation_id):
        touched.setdefault(str(entity_path), entity_path)

    count = 0
    for entity_path in touched.values():
        try:
            entity_id = str(load_markdown(entity_path).frontmatter.get("id") or "").strip()
        except Exception:
            entity_id = ""
        payload = {
            "vault": str(vault),
            "entity_path": str(entity_path),
            "entity_id": entity_id or str(entity_path),
            "draft_path": str(result.draft_path or ""),
            "transcript_path": str(result.transcript_path or ""),
            "conversation_id": conversation_id,
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "")}
        try:
            enqueue_job("entity.rewrite_story", payload, priority=priority_for_job_type("entity.rewrite_story"), db_path=db_path)
            count += 1
        except Exception:
            continue
    return count


def _entities_named_in_conversation(vault: Path, conversation_id, limit: int = 8) -> list[Path]:
    """Existing entity files whose canonical name or first name appears in the
    last few turns of this conversation. Bounded and deterministic — coalescing
    on the queue collapses repeat triggers for the same entity."""
    if not conversation_id:
        return []
    try:
        from ..frontmatter import load_markdown
        from .narrative_state import conversation_history

        turns = conversation_history(vault, conversation_id)
    except Exception:
        return []
    if not turns:
        return []
    window = " ".join(str(t.get("text") or "") for t in turns[-limit:]).lower()
    if not window.strip():
        return []
    found: list[Path] = []
    for entity_path in (vault / "entities").rglob("*.md"):
        try:
            fm = load_markdown(entity_path).frontmatter
        except Exception:
            continue
        name = str(fm.get("canonical_name") or "").strip()
        if not name:
            continue
        # full name, or a distinctive first name (>=4 chars to avoid "Al"/"Jo")
        first = name.split()[0]
        if name.lower() in window or (len(first) >= 4 and f" {first.lower()} " in f" {window} "):
            found.append(entity_path)
    return found



def run_jobs_worker(
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    worker_id: str | None = None,
    max_jobs: int | None = None,
    job_types: set[str] | None = None,
) -> dict[str, Any]:
    """Drain the queue once and return — claims jobs until none remain (it does
    not sleep waiting for new work). ``job_types`` restricts which job types are
    claimed; ``max_jobs`` bounds how many are processed."""
    db_path = db_path or sqlite_path()
    worker_id = worker_id or f"worker.{_iso().replace(':', '').replace('-', '').replace('Z', '')}.{uuid.uuid4().hex[:8]}"
    processed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []

    reclaimed = reclaim_stale_running_jobs(db_path)
    if reclaimed:
        try:
            from .log import get_logger

            get_logger(vault or vault_root()).info(f"jobs.reclaimed_stale count={reclaimed}")
        except Exception:
            pass
    while True:
        _promote_ready_retry_wait_jobs(db_path)
        job = claim_next_job(worker_id, db_path=db_path, job_types=job_types)
        if job is None:
            break
        try:
            result = dispatch_job(job, vault=vault, db_path=db_path, provider=provider, model=model)
            result_data, result_ref = _normalize_job_result(result)
            updated = mark_job_succeeded(job["id"], result=result_data, result_ref=result_ref, db_path=db_path)
            successes.append(updated or job)
            processed.append(updated or job)
            _requeue_recurring(job, db_path=db_path)
            _record_self_episode(vault, updated or job, db_path=db_path)
        except Exception as exc:
            updated = mark_job_failed(job["id"], str(exc), retry=True, db_path=db_path)
            failures.append(updated or job)
            processed.append(updated or job)
            # Only reschedule once the failure is terminal — retry_wait means
            # this occurrence is still in flight and will be retried.
            if updated is not None and str(updated.get("status")) == "failed":
                _record_self_episode(vault, updated, db_path=db_path)
                _requeue_recurring(job, db_path=db_path)
                if str(job.get("job_type")) == "plan.run":
                    from .plans import handle_terminal_failure

                    try:
                        handle_terminal_failure(job, vault=vault, db_path=db_path)
                    except Exception:
                        pass
        if max_jobs is not None and len(processed) >= max_jobs:
            break

    return {
        "worker_id": worker_id,
        "processed_count": len(processed),
        "success_count": len(successes),
        "failure_count": len(failures),
        "processed_jobs": processed,
        "successes": successes,
        "failures": failures,
    }


def _record_self_episode(vault: Path | None, job: dict[str, Any], db_path: Path | None = None) -> None:
    """A finished biography-grade job becomes a first-person episode (WO-4).
    Best-effort by design: autobiography must never break job processing."""
    try:
        from ..paths import vault_root
        from .self_episodes import record_job_episode

        record_job_episode(vault or vault_root(), job, db_path=db_path)
    except Exception:
        pass


def _requeue_recurring(job: dict[str, Any], *, db_path: Path | None = None) -> str | None:
    """After a recurring job completes (or fails terminally), enqueue its next
    occurrence. Computed from *now*, so downtime never produces a pile of
    missed instances — the schedule just resumes. Non-fatal by design: a bad
    recurrence rule stops the series rather than the worker."""
    recurrence = str(job.get("recurrence") or "").strip()
    if not recurrence:
        return None
    from .log import log_error
    from .scheduler import _to_iso, next_occurrence

    try:
        fire_at = next_occurrence(recurrence)
        payload = dict(job.get("payload") or {}) if isinstance(job.get("payload"), dict) else {}
        payload["due"] = _to_iso(fire_at)
        return enqueue_job(
            str(job.get("job_type")),
            payload,
            priority=int(job.get("priority") or 100),
            scheduled_for=fire_at,
            max_attempts=int(job.get("max_attempts") or 3),
            recurrence=recurrence,
            db_path=db_path,
        )
    except Exception as exc:
        try:
            log_error(vault_root(), f"requeue_recurring failed for {job.get('id')}", exc)
        except Exception:
            pass
        return None


def _promote_ready_retry_wait_jobs(db_path: Path | None = None) -> int:
    """Best-effort: a transient 'database is locked' here killed the live
    jobs service mid-ingestion (2026-07-05) and orphaned its claims. The
    next cycle promotes whatever this one could not."""
    try:
        conn = _connect(db_path)
    except Exception:
        return 0
    try:
        ensure_jobs_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        promoted = _promote_due_retry_wait_jobs(conn)
        conn.commit()
        return promoted
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def reclaim_stale_running_jobs(db_path: Path | None = None, *, stale_minutes: int = 45) -> int:
    """Jobs claimed by a worker that died stay 'running' forever — there is
    no liveness signal, so age is the heuristic. Requeued jobs re-run; every
    handler is idempotent (compaction is guarded, indexing converges), which
    is what makes this safe."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        conn = _connect(db_path)
    except Exception:
        return 0
    try:
        ensure_jobs_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running' AND started_at IS NOT NULL AND started_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount or 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def audit_jobs(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    db_path = db_path or sqlite_path()
    jobs = list_jobs(limit=5000, db_path=db_path)
    counts_by_status: dict[str, int] = {}
    counts_by_type: dict[str, int] = {}
    for job in jobs:
        counts_by_status[job["status"]] = counts_by_status.get(job["status"], 0) + 1
        counts_by_type[job["job_type"]] = counts_by_type.get(job["job_type"], 0) + 1

    failed_jobs = [job for job in jobs if job["status"] == "failed"]
    retry_wait_jobs = [job for job in jobs if job["status"] == "retry_wait"]
    running_jobs = [job for job in jobs if job["status"] == "running"]
    long_running_jobs = [job for job in running_jobs if _is_long_running(job)]
    stuck_jobs = [job for job in running_jobs if _is_stuck(job, DEFAULT_STUCK_TIMEOUT_MINUTES)]
    analyst_success = _last_success(job_type="analyst.scan", db_path=db_path)
    dreamer_success = _last_success(job_type="dreamer.maintenance", db_path=db_path)
    waiting_for_index = [
        job
        for job in jobs
        if job["job_type"] == "index.rebuild_record"
        and job["status"] in {"queued", "retry_wait", "running"}
    ]

    return {
        "counts_by_status": counts_by_status,
        "counts_by_type": counts_by_type,
        "queued_by_type": _counts_by_type([job for job in jobs if job["status"] == "queued"]),
        "failed_jobs": failed_jobs,
        "retry_wait_jobs": retry_wait_jobs,
        "long_running_jobs": long_running_jobs,
        "stuck_jobs": stuck_jobs,
        "stuck_timeout_minutes": DEFAULT_STUCK_TIMEOUT_MINUTES,
        "last_successful_analyst": analyst_success,
        "last_successful_dreamer": dreamer_success,
        "memory_records_waiting_for_index_rebuild": waiting_for_index,
    }


def _counts_by_type(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job["job_type"]] = counts.get(job["job_type"], 0) + 1
    return counts


def _is_long_running(job: dict[str, Any]) -> bool:
    return _running_duration_minutes(job) >= _LONG_RUNNING_MINUTES


def _is_stuck(job: dict[str, Any], timeout_minutes: int) -> bool:
    return _running_duration_minutes(job) >= timeout_minutes


def _running_duration_minutes(job: dict[str, Any]) -> float:
    started_at = str(job.get("started_at") or "")
    if not started_at:
        return 0.0
    started = _parse_timestamp(started_at)
    if started is None:
        return 0.0
    return (_now() - started).total_seconds() / 60.0




def _last_success(job_type: str, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        row = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE job_type = ? AND status = 'succeeded'
            ORDER BY finished_at DESC, created_at DESC
            LIMIT 1
            """,
            (job_type,),
        ).fetchone()
        return _row_to_job(row)
    finally:
        conn.close()


def format_job(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    payload_summary = payload.get("path") or payload.get("pattern_path") or payload.get("conversation_id") or payload.get("text") or ""
    if isinstance(payload_summary, str) and len(payload_summary) > 80:
        payload_summary = payload_summary[:77] + "..."
    coalesce_key = job.get("coalesce_key") or "-"
    coalesced_count = job.get("coalesced_count", 0)
    batch_id = job.get("batch_id") or payload.get("batch_id") or "-"
    return (
        f"{job['id']} | {job['job_type']} | {job['status']} | prio={job['priority']} | "
        f"attempts={job['attempts']}/{job['max_attempts']} | batch={batch_id} | coalesce={coalesce_key} | merged={coalesced_count} | "
        f"scheduled={job.get('scheduled_for') or '-'} | "
        f"{payload_summary}"
    ).rstrip()


def format_job_list(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "No jobs."
    return "\n".join(format_job(job) for job in jobs)


def format_job_audit(report: dict[str, Any]) -> str:
    lines = ["Job Audit", ""]
    lines.append("Queued jobs by type:")
    queued = report.get("queued_by_type", {})
    if queued:
        for job_type in sorted(queued):
            lines.append(f"- {job_type}: {queued[job_type]}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Failed jobs:")
    failed_jobs = report.get("failed_jobs", [])
    if failed_jobs:
        for job in failed_jobs:
            lines.append(f"- {job['id']} | {job['job_type']} | {job.get('error') or 'no error text'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Retry-wait jobs:")
    retry_wait_jobs = report.get("retry_wait_jobs", [])
    if retry_wait_jobs:
        for job in retry_wait_jobs:
            lines.append(f"- {job['id']} | {job['job_type']} | next={job.get('scheduled_for') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Long-running jobs:")
    long_running = report.get("long_running_jobs", [])
    if long_running:
        for job in long_running:
            lines.append(f"- {job['id']} | {job['job_type']} | started={job.get('started_at') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append(f"Stuck jobs (>{report.get('stuck_timeout_minutes', DEFAULT_STUCK_TIMEOUT_MINUTES)}m):")
    stuck_jobs = report.get("stuck_jobs", [])
    if stuck_jobs:
        for job in stuck_jobs:
            lines.append(f"- {job['id']} | {job['job_type']} | started={job.get('started_at') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Last successful Analyst run:")
    analyst = report.get("last_successful_analyst")
    if analyst:
        lines.append(f"- {analyst['id']} | finished={analyst.get('finished_at') or '-'} | ref={analyst.get('result_ref') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Last successful Dreamer run:")
    dreamer = report.get("last_successful_dreamer")
    if dreamer:
        lines.append(f"- {dreamer['id']} | finished={dreamer.get('finished_at') or '-'} | ref={dreamer.get('result_ref') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Memory records waiting for index rebuild:")
    waiting = report.get("memory_records_waiting_for_index_rebuild", [])
    if waiting:
        for job in waiting:
            lines.append(f"- {job['id']} | {job['status']} | {job.get('scheduled_for') or '-'}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def reap_stuck_jobs(
    *,
    db_path: Path | None = None,
    timeout_minutes: int = DEFAULT_STUCK_TIMEOUT_MINUTES,
    retry: bool = True,
) -> dict[str, Any]:
    db_path = db_path or sqlite_path()
    conn = _connect(db_path)
    try:
        ensure_jobs_table(conn)
        stuck_rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'running'
            """,
        ).fetchall()
        reaped: list[dict[str, Any]] = []
        for row in stuck_rows:
            job = _row_to_job(row)
            if job is None:
                continue
            if not _is_stuck(job, timeout_minutes):
                continue
            updated = mark_job_failed(job["id"], f"stuck running job exceeded {timeout_minutes} minutes", retry=retry, db_path=db_path)
            if updated is not None:
                reaped.append(updated)
        return {
            "timeout_minutes": timeout_minutes,
            "retry": retry,
            "reaped_count": len(reaped),
            "reaped_jobs": reaped,
        }
    finally:
        conn.close()


def _find_pattern_path(vault: Path, pattern_id: str) -> Path | None:
    patterns_root = vault / "patterns"
    if not patterns_root.exists():
        return None
    for path in sorted(patterns_root.glob("*.md")):
        try:
            from ..frontmatter import load_markdown

            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("id") or "") == pattern_id:
            return path
    return None
