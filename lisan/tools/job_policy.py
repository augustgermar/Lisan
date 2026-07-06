from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import sqlite_path
from ..utils import parse_utc_timestamp as _parse_timestamp


DEFAULT_JOB_PRIORITIES = {
    "ingest.reindex_artifact": 40,
    "ingest.parse_file": 45,
    "ingest.extract_evidence": 50,
    "ingest.scan_path": 55,
    "writer.extract_turn": 20,
    "skeptic.review_pattern": 30,
    "index.rebuild_record": 40,
    "index.rebuild_all": 40,
    "index.embed_pending": 45,
    "ingest.artifact.extract": 50,
    "pattern.audit": 60,
    "manifest.regenerate": 90,
    "analyst.scan": 70,
    "dreamer.maintenance": 80,
    "deviation.scan": 85,
    "self.evaluate": 90,
    "entity.rewrite_story": 85,
    # User-scheduled tasks outrank maintenance: when a reminder and a dreamer
    # pass are both due, the reminder fires first.
    "task.reminder": 10,
    "task.prompt": 12,
    "task.run_codex": 12,
    "plan.run": 15,
    "capture.observe": 22,
}

COALESCE_AGGRESSIVE = {
    "analyst.scan",
    "dreamer.maintenance",
    "deviation.scan",
    "self.evaluate",
    "pattern.audit",
    "manifest.regenerate",
    "index.rebuild_all",
}

COALESCE_BY_RECORD = {
    "index.rebuild_record",
    "skeptic.review_pattern",
    "entity.rewrite_story",
}

NO_COALESCE = {
    "writer.extract_turn",
    "ingest.file.parse",
    "ingest.artifact.extract",
    # Every scheduled task is a distinct commitment; two reminders must never
    # merge into one.
    "task.reminder",
    "task.prompt",
    "task.run_codex",
    "plan.run",
    "capture.observe",
}

DEFAULT_STUCK_TIMEOUT_MINUTES = 15
DEFAULT_ANALYST_DELTA_THRESHOLD = 5
DEFAULT_DREAMER_INTERVAL_HOURS = 24


def priority_for_job_type(job_type: str) -> int:
    return int(DEFAULT_JOB_PRIORITIES.get(job_type, 100))


def should_coalesce(job_type: str) -> bool:
    return job_type in COALESCE_AGGRESSIVE or job_type in COALESCE_BY_RECORD


def coalesce_key_for_job(job_type: str, payload: dict[str, Any] | None) -> str | None:
    payload = payload or {}
    if job_type in NO_COALESCE:
        return None
    if job_type == "ingest.scan_path":
        path = str(payload.get("path") or payload.get("source_path") or payload.get("root") or "").strip()
        if path:
            return f"{job_type}|path={path}"
        return f"{job_type}|global"
    if job_type in {"ingest.parse_file", "ingest.extract_evidence", "ingest.reindex_artifact"}:
        identifier = _record_identifier(payload) or str(payload.get("source_path") or "").strip()
        if identifier:
            return f"{job_type}|record={identifier}"
        return f"{job_type}|global"
    if job_type in COALESCE_AGGRESSIVE:
        vault = str(payload.get("vault") or payload.get("vault_path") or "").strip()
        if vault:
            return f"{job_type}|vault={vault}"
        return f"{job_type}|global"
    if job_type == "entity.rewrite_story":
        entity_id = str(payload.get("entity_id") or payload.get("entity_path") or "").strip()
        if entity_id:
            return f"{job_type}|entity_id={entity_id}"
        return f"{job_type}|global"
    if job_type in COALESCE_BY_RECORD:
        record_id = _record_identifier(payload)
        if record_id:
            return f"{job_type}|record={record_id}"
        return f"{job_type}|global"
    return None


def unique_group_for_job(job_type: str, payload: dict[str, Any] | None) -> str | None:
    payload = payload or {}
    if job_type in NO_COALESCE:
        return None
    if job_type == "ingest.scan_path":
        path = str(payload.get("path") or payload.get("source_path") or payload.get("root") or "").strip()
        if path:
            return f"path:{path}"
        return "path:global"
    if job_type in {"ingest.parse_file", "ingest.extract_evidence", "ingest.reindex_artifact"}:
        identifier = _record_identifier(payload) or str(payload.get("source_path") or "").strip()
        if identifier:
            return f"record:{identifier}"
        return "record:global"
    if job_type == "entity.rewrite_story":
        entity_id = str(payload.get("entity_id") or payload.get("entity_path") or "").strip()
        if entity_id:
            return f"entity:{entity_id}"
        return "entity:global"
    if job_type in COALESCE_BY_RECORD:
        record_id = _record_identifier(payload)
        if record_id:
            return f"record:{record_id}"
        return "record:global"
    if job_type in COALESCE_AGGRESSIVE:
        vault = str(payload.get("vault") or payload.get("vault_path") or "").strip()
        if vault:
            return f"maintenance:{vault}"
        return f"maintenance:{job_type}"
    return None


def should_run_synchronously(job_type: str, turn_metadata: dict[str, Any] | None) -> bool:
    turn_metadata = turn_metadata or {}
    if job_type == "writer.extract_turn":
        return is_memory_critical(turn_metadata)
    if job_type == "index.rebuild_record":
        return is_memory_critical(turn_metadata) and bool(turn_metadata.get("synchronous_index_refresh", False))
    return False


def is_memory_critical(turn_metadata: dict[str, Any] | None) -> bool:
    turn_metadata = turn_metadata or {}
    if bool(turn_metadata.get("explicit_memory_request", False)):
        return True
    intent = str(turn_metadata.get("memory_intent") or "").lower()
    if intent in {"remember", "correction", "fact_correction"}:
        return True
    text = str(turn_metadata.get("text") or "").strip().lower()
    if text.startswith("/remember") or text.startswith("/forget"):
        return True
    if "remember this" in text or "fact correction" in text:
        return True
    return False


def should_enqueue_after_turn(turn_metadata: dict[str, Any] | None, db_path: Path | None = None) -> bool:
    return bool(which_jobs_for_turn(turn_metadata or {}, db_path=db_path))


def which_jobs_for_turn(turn_metadata: dict[str, Any] | None, db_path: Path | None = None) -> list[dict[str, Any]]:
    turn_metadata = turn_metadata or {}
    if not turn_metadata:
        return []
    text = str(turn_metadata.get("text") or "")
    lowered = text.lower().strip()
    if bool(turn_metadata.get("fast_path_used", False)):
        return []
    if str(turn_metadata.get("turn_classification") or "").lower() in {"identity", "help", "status", "ack", "smalltalk", "skip"}:
        return []
    if len(lowered) <= 5:
        return []
    if str(turn_metadata.get("action") or "") == "skip":
        return []
    if str(turn_metadata.get("mode") or "") == "skip":
        return []

    db_path = db_path or _turn_db_path(turn_metadata)
    jobs: list[dict[str, Any]] = []

    if turn_metadata.get("records_written", 0) or turn_metadata.get("draft_path"):
        jobs.append(_job_spec("index.rebuild_record", turn_metadata, priority_for_job_type("index.rebuild_record")))

    if _should_queue_analyst(turn_metadata, db_path):
        jobs.append(_job_spec("analyst.scan", turn_metadata, priority_for_job_type("analyst.scan")))

    if _should_queue_self_eval(db_path):
        jobs.append(_job_spec("self.evaluate", turn_metadata, priority_for_job_type("self.evaluate")))

    if _should_queue_deviation_scan(db_path):
        jobs.append(_job_spec("deviation.scan", turn_metadata, priority_for_job_type("deviation.scan")))

    if _should_queue_dreamer(turn_metadata, db_path):
        jobs.append(_job_spec("dreamer.maintenance", turn_metadata, priority_for_job_type("dreamer.maintenance"), extra={"task": "compress"}))

    return jobs


def _job_spec(job_type: str, turn_metadata: dict[str, Any], priority: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "vault": str(turn_metadata.get("vault") or ""),
        "conversation_id": turn_metadata.get("conversation_id"),
        "reason": str(turn_metadata.get("reason") or ""),
        "text": str(turn_metadata.get("text") or ""),
    }
    payload.update(extra or {})
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    return {
        "job_type": job_type,
        "payload": payload,
        "priority": priority,
        "max_attempts": 3,
    }


def _turn_db_path(turn_metadata: dict[str, Any]) -> Path | None:
    value = turn_metadata.get("db_path")
    if value:
        return Path(str(value))
    return sqlite_path()


def _should_queue_analyst(turn_metadata: dict[str, Any], db_path: Path | None) -> bool:
    if bool(turn_metadata.get("self_analysis_requested", False)):
        return True
    if bool(turn_metadata.get("high_salience", False)):
        return True
    if _changed_records_since_last_job("analyst.scan", db_path) >= DEFAULT_ANALYST_DELTA_THRESHOLD:
        return True
    return False


def _should_queue_self_eval(db_path: Path | None) -> bool:
    """Weekly, off the same post-turn idle seam. The interval lives in
    config (self_eval.interval_hours) so the owner can tune the cadence."""
    from ..config import load_config
    from .self_eval import self_eval_config

    cfg = self_eval_config(load_config())
    if not cfg.get("enabled", True):
        return False
    last = _last_successful_job_time("self.evaluate", db_path)
    if last is None:
        return True
    return _hours_since(last) >= float(cfg.get("interval_hours") or 168)


def _should_queue_deviation_scan(db_path: Path | None) -> bool:
    """Once a day, off the same idle seam as the dreamer — no new daemon."""
    last = _last_successful_job_time("deviation.scan", db_path)
    if last is None:
        return True
    return _hours_since(last) >= 24


def _should_queue_dreamer(turn_metadata: dict[str, Any], db_path: Path | None) -> bool:
    if _reviewed_patterns_exist(db_path):
        return True
    if _stale_claims_exist(db_path):
        return True
    last = _last_successful_job_time("dreamer.maintenance", db_path)
    if last is None:
        return True
    return _hours_since(last) >= DEFAULT_DREAMER_INTERVAL_HOURS


def _changed_records_since_last_job(job_type: str, db_path: Path | None) -> int:
    if db_path is None:
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        last = _last_successful_job_time(job_type, db_path, conn)
        if last is None:
            try:
                row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
                return int(row[0] or 0) if row else 0
            except sqlite3.OperationalError:
                return 0
        cutoff = _job_date(last)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM files
                WHERE COALESCE(updated, created) >= ?
                """,
                (cutoff,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _reviewed_patterns_exist(db_path: Path | None) -> bool:
    if db_path is None:
        return False
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM files AS pattern
                WHERE pattern.type = 'pattern'
                  AND pattern.status IN ('skeptic_reviewed', 'supported', 'integrated')
                  AND EXISTS (
                      SELECT 1
                      FROM files AS review
                      WHERE review.type = 'skeptical_review'
                        AND review.reviewed_record_id = pattern.id
                        AND COALESCE(review.approved, 0) = 1
                  )
                """
            ).fetchone()
            return bool(row and row[0])
        except sqlite3.OperationalError:
            return False
    finally:
        conn.close()


def _stale_claims_exist(db_path: Path | None) -> bool:
    if db_path is None:
        return False
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM files
                WHERE type = 'claim'
                  AND (
                    status IN ('stale', 'disputed', 'rejected')
                    OR COALESCE(review_after, '') < ?
                  )
                """,
                (today,),
            ).fetchone()
            return bool(row and row[0])
        except sqlite3.OperationalError:
            return False
    finally:
        conn.close()


def _last_successful_job_time(job_type: str, db_path: Path | None, conn: sqlite3.Connection | None = None) -> datetime | None:
    close_conn = False
    if conn is None:
        if db_path is None:
            return None
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        close_conn = True
    try:
        try:
            row = conn.execute(
                """
                SELECT finished_at
                FROM jobs
                WHERE job_type = ? AND status = 'succeeded' AND finished_at IS NOT NULL
                ORDER BY finished_at DESC, created_at DESC
                LIMIT 1
                """,
                (job_type,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        return _parse_timestamp(str(row[0]))
    finally:
        if close_conn and conn is not None:
            conn.close()




def _hours_since(dt: datetime) -> float:
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _job_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).date().isoformat()


def _record_identifier(payload: dict[str, Any]) -> str | None:
    for key in ("artifact_id", "record_id", "pattern_id", "path", "source_path", "job_target", "target_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None
