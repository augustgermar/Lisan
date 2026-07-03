"""Ingestion: external files become memory.

Two layers, only one of them format-specific. EXTRACT turns any source file
into text plus metadata (per-format, kept trivial); PROCESS turns that text
into records the pipeline understands. Two modes ride on those layers:

- Artifact ingestion — scan a path, parse files, extract evidence records
  through the quarantinable batch pipeline (scan_path / parse_file /
  extract_evidence, driven by the ingest.* job types).
- Reference ingestion — chunk documents (markdown, text, PDF) into knowledge
  records with source attribution and deterministic entity linking, so
  ingested reference material participates in narrative memory
  (ingest_reference_sources).
"""
from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import FrontmatterError, load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import slugify
from .document_chunker import Chunk, chunk_document
from .entity_kind import assign_kind
from .record_factory import new_artifact, new_claim, new_evidence, new_entity, new_knowledge
from .ingest_batches import create_batch, update_batch_status, summarize_batch, list_batches, get_batch, artifacts_for_batch, jobs_for_batch, manifest_rows_for_batch, quarantine_batch


INGESTION_MANIFEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingestion_manifest (
    source_path TEXT PRIMARY KEY,
    artifact_hash TEXT,
    last_seen TEXT,
    last_ingested TEXT,
    status TEXT NOT NULL,
    artifact_id TEXT,
    batch_id TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingestion_manifest_status
    ON ingestion_manifest(status, last_seen);

CREATE INDEX IF NOT EXISTS idx_ingestion_manifest_hash
    ON ingestion_manifest(artifact_hash);

CREATE INDEX IF NOT EXISTS idx_ingestion_manifest_batch
    ON ingestion_manifest(batch_id, status);
"""

SUPPORTED_TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".csv"}
SUPPORTED_BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "env", ".tox"}
EXCLUDED_NAME_EXACT = {
    ".env",
    "id_rsa",
    "id_dsa",
    "password-store",
    "Passwords",
    "login.keychain-db",
}
EXCLUDED_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore"}

SECRET_KEYWORDS = {
    "password",
    "credentials",
    "secret",
    "token",
    "private key",
    "api key",
    "ssh key",
    "auth",
}

FINANCIAL_KEYWORDS = {
    "bank",
    "account",
    "tax",
    "salary",
    "invoice",
    "payment",
    "budget",
    "transaction",
    "credit card",
}

LEGAL_KEYWORDS = {
    "legal",
    "lawyer",
    "attorney",
    "court",
    "divorce",
    "custody",
    "lawsuit",
    "contract",
}

HEALTH_KEYWORDS = {
    "health",
    "medical",
    "diagnosis",
    "therapy",
    "doctor",
    "medication",
    "symptom",
}

WORK_KEYWORDS = {
    "work",
    "project",
    "team",
    "manager",
    "client",
    "system",
    "infrastructure",
    "deployment",
    "release",
    "rollout",
}

FACT_MARKERS = {
    " asked ",
    " said ",
    " stated ",
    " noted ",
    " confirmed ",
    " scheduled ",
    " sent ",
    " updated ",
    " approved ",
    " rejected ",
    " met ",
    " requested ",
    " reported ",
    " attached ",
}

CLAIM_MARKERS = {
    "i think",
    "maybe",
    "might",
    "seems",
    "appears",
    "probably",
    "likely",
    "should",
    "need to",
    "always",
    "never",
    "feel",
    "feels",
    "worried",
    "hoping",
}


def ensure_ingestion_manifest_table(conn: sqlite3.Connection) -> None:
    conn.executescript(INGESTION_MANIFEST_SCHEMA_SQL)
    _ensure_manifest_columns(conn)


def _ensure_manifest_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(ingestion_manifest)").fetchall()}
    if "batch_id" not in existing:
        conn.execute("ALTER TABLE ingestion_manifest ADD COLUMN batch_id TEXT")


def _normalize_ingest_filters(
    *,
    include_ext: list[str] | None = None,
    exclude_ext: list[str] | None = None,
) -> dict[str, set[str]]:
    return {
        "include_ext": {ext.strip().lower() for ext in (include_ext or []) if ext and ext.strip()},
        "exclude_ext": {ext.strip().lower() for ext in (exclude_ext or []) if ext and ext.strip()},
    }


def _manifest_snapshot(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute("SELECT * FROM ingestion_manifest").fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _build_manifest_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_path = str(row.get("source_path") or "")
        if source_path:
            by_source[source_path] = row
        artifact_hash = str(row.get("artifact_hash") or "")
        if artifact_hash:
            by_hash.setdefault(artifact_hash, []).append(row)
    return {"by_source": by_source, "by_hash": by_hash}


def _looks_secret_like(plan: dict[str, Any]) -> bool:
    reason = str(plan.get("skip_reason") or "").lower()
    sensitivity = str(plan.get("sensitivity") or "").lower()
    return "secret" in reason or "credential" in reason or sensitivity == "sealed"


def scan_path(
    path: Path,
    vault: Path | None = None,
    db_path: Path | None = None,
    queue_jobs: bool = True,
    max_file_size_bytes: int | None = None,
    *,
    dry_run: bool = False,
    include_ext: list[str] | None = None,
    exclude_ext: list[str] | None = None,
    include_hidden: bool = False,
    allow_restricted: bool = False,
    allow_high: bool = False,
    allow_sealed: bool = False,
    batch_mode: str = "scan",
    batch_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = load_config()
    ingest_cfg = config.get("ingest", {}) if isinstance(config, dict) else {}
    max_file_size_bytes = int(max_file_size_bytes or ingest_cfg.get("max_file_size_bytes") or 5 * 1024 * 1024)
    skip_if_inside_vault = bool(ingest_cfg.get("skip_if_inside_vault", True))

    filters = _normalize_ingest_filters(include_ext=include_ext, exclude_ext=exclude_ext)
    root = path.resolve()
    scan_paths = [root] if root.is_file() else _walk_files(root, include_hidden=include_hidden)

    if dry_run:
        return plan_scan_path(
            path,
            vault=vault,
            db_path=db_path,
            max_file_size_bytes=max_file_size_bytes,
            include_ext=include_ext,
            exclude_ext=exclude_ext,
            include_hidden=include_hidden,
            allow_restricted=allow_restricted,
            allow_high=allow_high,
            allow_sealed=allow_sealed,
            batch_mode=batch_mode,
            batch_options=_scan_batch_options(
                path,
                vault=vault,
                db_path=db_path,
                max_file_size_bytes=max_file_size_bytes,
                include_ext=include_ext,
                exclude_ext=exclude_ext,
                include_hidden=include_hidden,
                allow_restricted=allow_restricted,
                allow_high=allow_high,
                allow_sealed=allow_sealed,
                batch_mode=batch_mode,
            ),
        )

    conn = _connect(db_path)
    try:
        ensure_ingestion_manifest_table(conn)
        batch_options = _scan_batch_options(
            path,
            vault=vault,
            db_path=db_path,
            max_file_size_bytes=max_file_size_bytes,
            include_ext=include_ext,
            exclude_ext=exclude_ext,
            include_hidden=include_hidden,
            allow_restricted=allow_restricted,
            allow_high=allow_high,
            allow_sealed=allow_sealed,
            batch_mode=batch_mode,
        )
        active_batch_id = batch_id or create_batch(str(root), batch_mode, batch_options, requested_by=requested_by, db_path=db_path, status="planned")
        update_batch_status(active_batch_id, "running", summary={"root": str(root), "mode": batch_mode}, db_path=db_path)
        try:
            manifest_changes: list[dict[str, Any]] = []
            queued_jobs: list[dict[str, Any]] = []
            discovered = skipped = parsed = extracted = failed = 0
            for source_path in scan_paths:
                result = _ingest_single_file(
                    source_path,
                    vault=vault,
                    db_path=db_path,
                    conn=conn,
                    queue_jobs=queue_jobs,
                    max_file_size_bytes=max_file_size_bytes,
                    skip_if_inside_vault=skip_if_inside_vault,
                    include_hidden=include_hidden,
                    allow_restricted=allow_restricted,
                    allow_high=allow_high,
                    allow_sealed=allow_sealed,
                    filters=filters,
                    batch_id=active_batch_id,
                )
                manifest_changes.append(result)
                status = str(result["status"])
                if status == "skipped":
                    skipped += 1
                elif status == "failed":
                    failed += 1
                elif status == "parsed":
                    parsed += 1
                elif status == "evidence_extracted":
                    extracted += 1
                elif status == "discovered":
                    discovered += 1
                if result.get("queued_jobs"):
                    queued_jobs.extend(result["queued_jobs"])
            batch_status = "completed_with_errors" if failed else "completed"
            batch_summary = {
                "root": str(root),
                "mode": batch_mode,
                "scanned_count": len(scan_paths),
                "discovered": discovered,
                "parsed": parsed,
                "evidence_extracted": extracted,
                "failed": failed,
                "skipped": skipped,
                "queued_jobs": len(queued_jobs),
            }
            update_batch_status(active_batch_id, batch_status, summary=batch_summary, db_path=db_path)
            conn.commit()
            return {
                "root": str(root),
                "vault": str(vault),
                "batch_id": active_batch_id,
                "batch_summary": batch_summary,
                "scanned_count": len(scan_paths),
                "discovered": discovered,
                "parsed": parsed,
                "evidence_extracted": extracted,
                "failed": failed,
                "skipped": skipped,
                "queued_jobs": queued_jobs,
                "manifest_changes": manifest_changes,
            }
        except Exception as exc:
            update_batch_status(active_batch_id, "failed", summary={"root": str(root), "mode": batch_mode}, error=str(exc), db_path=db_path)
            conn.rollback()
            raise
    finally:
        conn.close()


def plan_scan_path(
    path: Path,
    vault: Path | None = None,
    db_path: Path | None = None,
    max_file_size_bytes: int | None = None,
    *,
    include_ext: list[str] | None = None,
    exclude_ext: list[str] | None = None,
    include_hidden: bool = False,
    allow_restricted: bool = False,
    allow_high: bool = False,
    allow_sealed: bool = False,
    batch_mode: str = "scan",
    batch_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = load_config()
    ingest_cfg = config.get("ingest", {}) if isinstance(config, dict) else {}
    max_file_size_bytes = int(max_file_size_bytes or ingest_cfg.get("max_file_size_bytes") or 5 * 1024 * 1024)
    skip_if_inside_vault = bool(ingest_cfg.get("skip_if_inside_vault", True))
    filters = _normalize_ingest_filters(include_ext=include_ext, exclude_ext=exclude_ext)
    root = path.resolve()
    scan_paths = [root] if root.is_file() else _walk_files(root, include_hidden=include_hidden)
    manifest_snapshot = _manifest_snapshot(db_path)
    duplicate_hash_map: dict[str, list[str]] = {}
    planned_files: list[dict[str, Any]] = []
    summary = {
        "total_files_seen": len(scan_paths),
        "would_ingest": 0,
        "would_skip": 0,
        "would_parse": 0,
        "would_create_artifact_only": 0,
        "would_enqueue_parse_jobs": 0,
        "would_enqueue_evidence_extraction_jobs": 0,
        "changed_files_already_known_in_manifest": 0,
        "unsupported_file_types": 0,
        "skipped_secret_like_files": 0,
        "high_count": 0,
        "restricted_count": 0,
        "sealed_count": 0,
        "new": 0,
        "unchanged": 0,
        "changed": 0,
        "skipped": 0,
        "unsupported": 0,
        "duplicate_hash": 0,
    }

    for source_path in scan_paths:
        plan = _plan_ingest_file(
            source_path,
            vault=vault,
            db_path=db_path,
            manifest_snapshot=manifest_snapshot,
            max_file_size_bytes=max_file_size_bytes,
            skip_if_inside_vault=skip_if_inside_vault,
            include_hidden=include_hidden,
            allow_restricted=allow_restricted,
            allow_high=allow_high,
            allow_sealed=allow_sealed,
            filters=filters,
        )
        planned_files.append(plan)
        classification = str(plan.get("classification") or "")
        if classification == "skipped":
            summary["would_skip"] += 1
            summary["skipped"] += 1
        elif classification == "unsupported":
            summary["unsupported"] += 1
            summary["unsupported_file_types"] += 1
        elif classification in {"new", "unchanged", "changed"}:
            summary[classification] += 1
        if plan.get("duplicate_hash"):
            summary["duplicate_hash"] += 1
        if plan.get("manifest_state") == "changed":
            summary["changed_files_already_known_in_manifest"] += 1
        if plan.get("sensitivity") == "high":
            summary["high_count"] += 1
        elif plan.get("sensitivity") == "restricted":
            summary["restricted_count"] += 1
        elif plan.get("sensitivity") == "sealed":
            summary["sealed_count"] += 1
        if classification == "skipped" and _looks_secret_like(plan):
            summary["skipped_secret_like_files"] += 1
        if plan.get("planned_action") in {"create_artifact_and_parse", "reuse_existing_pending_parse"}:
            summary["would_ingest"] += 1
        if plan.get("planned_action") == "create_artifact_only":
            summary["would_ingest"] += 1
            summary["would_create_artifact_only"] += 1
        if plan.get("will_parse"):
            summary["would_parse"] += 1
        if plan.get("would_enqueue"):
            if "ingest.parse_file" in plan["would_enqueue"]:
                summary["would_enqueue_parse_jobs"] += 1
            if "ingest.extract_evidence" in plan["would_enqueue"]:
                summary["would_enqueue_evidence_extraction_jobs"] += 1
        artifact_hash = str(plan.get("artifact_hash") or "")
        if artifact_hash:
            duplicate_hash_map.setdefault(artifact_hash, []).append(str(plan.get("source_path") or ""))

    duplicate_hashes = [
        {"artifact_hash": artifact_hash, "source_paths": sorted(set(paths))}
        for artifact_hash, paths in duplicate_hash_map.items()
        if len(set(paths)) > 1
    ]

    batch_options = batch_options or _scan_batch_options(
        path,
        vault=vault,
        db_path=db_path,
        max_file_size_bytes=max_file_size_bytes,
        include_ext=include_ext,
        exclude_ext=exclude_ext,
        include_hidden=include_hidden,
        allow_restricted=allow_restricted,
        allow_high=allow_high,
        allow_sealed=allow_sealed,
        batch_mode=batch_mode,
    )
    proposed_batch_summary = {
        "source_root": str(root),
        "mode": batch_mode,
        "status": "planned",
        "options": batch_options,
        "summary": summary,
    }

    return {
        "dry_run": True,
        "would_create_batch": False,
        "batch_options": batch_options,
        "proposed_batch_summary": proposed_batch_summary,
        "path": str(root),
        "vault": str(vault),
        "summary": summary,
        "files": planned_files,
        "duplicate_hashes": duplicate_hashes,
        "manifest_snapshot_size": len(manifest_snapshot),
    }


def parse_file(
    *,
    artifact_id: str,
    source_path: str,
    artifact_hash: str,
    file_name: str,
    file_ext: str,
    source_type: str,
    imported_at: str,
    modified_at: str,
    size_bytes: int,
    sensitivity: str,
    source_uri: str | None = None,
    batch_id: str | None = None,
    db_path: Path | None = None,
    vault: Path | None = None,
    extracted_text_ref: str | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    source = Path(source_path)
    if not source.exists():
        _update_manifest(source_path, db_path=db_path, status="failed", error="Source file is missing", artifact_id=artifact_id, artifact_hash=artifact_hash, batch_id=batch_id)
        raise FileNotFoundError(source)

    if source_type not in {"markdown", "text"} and file_ext.lower() not in {".md", ".markdown", ".txt", ".json", ".csv"}:
        _update_manifest(source_path, db_path=db_path, status="skipped", error="Unsupported file type for parse", artifact_id=artifact_id, artifact_hash=artifact_hash, batch_id=batch_id)
        return {"artifact_id": artifact_id, "status": "skipped", "reason": "unsupported"}

    text = _read_text_source(source)
    extracted_text_ref = extracted_text_ref or _extracted_text_path(vault, artifact_id)
    extracted_path = Path(extracted_text_ref)
    extracted_path.parent.mkdir(parents=True, exist_ok=True)
    extracted_path.write_text(text, encoding="utf-8")

    summary = _summarize_text(text, file_name=file_name)
    preview = text[: int(load_config().get("ingest", {}).get("text_preview_chars", 4000))]
    _update_artifact_record(
        vault=vault,
        artifact_id=artifact_id,
        updates={
            "ingestion_status": "parsed",
            "status": "parsed",
            "summary": summary,
            "extracted_text_ref": str(extracted_path),
        },
    )
    _update_manifest(
        source_path,
        db_path=db_path,
        status="parsed",
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        batch_id=batch_id,
        error=None,
        last_ingested=_iso_now(),
    )
    parsed = {
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "source_path": source_path,
        "source_type": source_type,
        "file_name": file_name,
        "file_ext": file_ext,
        "summary": summary,
        "extracted_text_ref": str(extracted_path),
        "text_preview": preview,
    }
    from .jobs import enqueue_job

    job_id = enqueue_job(
        "ingest.extract_evidence",
        {
            "vault": str(vault),
            "db_path": str(db_path),
            "artifact_id": artifact_id,
            "source_path": source_path,
            "artifact_hash": artifact_hash,
            "file_name": file_name,
            "file_ext": file_ext,
            "source_type": source_type,
            "source_uri": source_uri,
            "imported_at": imported_at,
            "modified_at": modified_at,
            "size_bytes": size_bytes,
            "sensitivity": sensitivity,
            "extracted_text_ref": str(extracted_path),
            "batch_id": batch_id,
        },
        batch_id=batch_id,
        db_path=db_path,
    )
    parsed["queued_jobs"] = [{"job_type": "ingest.extract_evidence", "job_id": job_id, "batch_id": batch_id}]
    return parsed


def extract_evidence(
    *,
    artifact_id: str,
    source_path: str,
    artifact_hash: str,
    file_name: str,
    file_ext: str,
    source_type: str,
    imported_at: str,
    modified_at: str,
    size_bytes: int,
    sensitivity: str,
    extracted_text_ref: str | None = None,
    source_uri: str | None = None,
    batch_id: str | None = None,
    db_path: Path | None = None,
    vault: Path | None = None,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    source = Path(source_path)
    if not source.exists():
        _update_manifest(source_path, db_path=db_path, status="failed", error="Source file is missing", artifact_id=artifact_id, artifact_hash=artifact_hash, batch_id=batch_id)
        raise FileNotFoundError(source)

    text = Path(extracted_text_ref).read_text(encoding="utf-8") if extracted_text_ref and Path(extracted_text_ref).exists() else _read_text_source(source)
    record_date = (modified_at or imported_at)[:10]
    evidence_title = f"{artifact_id} extracted evidence"
    facts, claim_lines = _extract_fact_and_claim_lines(text)
    if not facts:
        facts = [_summarize_text(text, file_name=file_name)]

    evidence_path = _ensure_evidence_record(
        vault=vault,
        title=evidence_title,
        record_date=record_date,
        source_type="document",
        source_uri=source_uri or f"file://{source_path}",
        artifact_ref=artifact_id,
        artifact_hash=artifact_hash,
        timestamp_of_artifact=modified_at or imported_at,
        actors=[],
        arena=_infer_arena(source_path, file_name, text),
        compartments=_classify_compartments(source_path, text),
        sensitivity=sensitivity,
        reliability=_reliability_for_sensitivity(sensitivity),
        privacy=_privacy_for_sensitivity(sensitivity),
        significance="low",
        summary=_summarize_text(text, file_name=file_name),
        observed_facts=[f"Document states: {fact}" for fact in facts[:5]],
        verbatim_excerpt=text[:1000],
        linked_claims=[],
        linked_episodes=[],
        batch_id=batch_id,
    )

    created_claims: list[str] = []
    for index, line in enumerate(claim_lines[:5]):
        claim_text = line.strip()
        claim_path = _ensure_claim_record(
            vault=vault,
            claim_text=claim_text,
            record_date=record_date,
            claim_class=_claim_class_for_text(claim_text),
            owner=_owner_for_text(source_path, claim_text),
            status="active",
            confidence=_claim_confidence_for_text(claim_text, sensitivity),
            supporting_evidence=[str(_load_record_id(evidence_path))],
            contradicting_evidence=[],
            linked_patterns=[],
            first_seen=record_date,
            last_reviewed=record_date,
            review_notes="Derived from imported artifact. The claim is a hypothesis about what the document states, not a verified fact.",
            source_type="document",
            source_uri=source_uri or f"file://{source_path}",
            artifact_ref=artifact_id,
            artifact_hash=artifact_hash,
            timestamp_of_artifact=modified_at or imported_at,
            arena=_infer_arena(source_path, file_name, text),
            compartments=_classify_compartments(source_path, text),
            privacy=_privacy_for_sensitivity(sensitivity),
            significance="low",
            summary=claim_text[:120],
            batch_id=batch_id,
        )
        created_claims.append(str(_load_record_id(claim_path)))

    _update_artifact_record(
        vault=vault,
        artifact_id=artifact_id,
        updates={
            "ingestion_status": "evidence_extracted",
            "status": "evidence_extracted",
            "summary": _summarize_text(text, file_name=file_name),
            "extracted_text_ref": str(extracted_text_ref or _extracted_text_path(vault, artifact_id)),
            "linked_evidence": [str(_load_record_id(evidence_path))],
            "linked_claims": created_claims,
            "parse_errors": [],
            "batch_id": batch_id,
        },
    )
    _update_manifest(
        source_path,
        db_path=db_path,
        status="evidence_extracted",
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        error=None,
        last_ingested=_iso_now(),
    )
    from .jobs import enqueue_job

    job_id = enqueue_job(
        "ingest.reindex_artifact",
        {
            "vault": str(vault),
            "db_path": str(db_path),
            "artifact_id": artifact_id,
            "source_path": source_path,
            "artifact_hash": artifact_hash,
            "batch_id": batch_id,
        },
        batch_id=batch_id,
        db_path=db_path,
    )
    return {
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "source_path": source_path,
        "evidence_id": str(_load_record_id(evidence_path)),
        "claim_ids": created_claims,
        "status": "evidence_extracted",
        "queued_jobs": [{"job_type": "ingest.reindex_artifact", "job_id": job_id, "batch_id": batch_id}],
    }


def reindex_artifact(
    *,
    artifact_id: str,
    source_path: str,
    artifact_hash: str,
    batch_id: str | None = None,
    db_path: Path | None = None,
    vault: Path | None = None,
) -> dict[str, Any]:
    from .rebuild_index import rebuild_index

    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    counts = rebuild_index(vault=vault, db_path=db_path)
    return {
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "source_path": source_path,
        "batch_id": batch_id,
        "status": "reindexed",
        "counts": counts,
    }


def list_manifest(status: str | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    db_path = db_path or sqlite_path()
    conn = _connect(db_path)
    try:
        ensure_ingestion_manifest_table(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM ingestion_manifest WHERE status = ? ORDER BY last_seen DESC, source_path ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ingestion_manifest ORDER BY last_seen DESC, source_path ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def show_artifact(artifact_id: str, vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any] | None:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    artifact_path = _find_artifact_path(vault, artifact_id)
    manifest = _manifest_by_artifact_id(db_path, artifact_id)
    if artifact_path is None:
        return {"artifact": None, "manifest": manifest}
    try:
        doc = load_markdown(artifact_path)
    except Exception:
        return {"artifact": None, "manifest": manifest, "path": str(artifact_path)}
    return {
        "artifact": {
            "path": str(artifact_path),
            "frontmatter": doc.frontmatter,
            "body": doc.body,
        },
        "manifest": manifest,
    }


def audit_ingestion(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    manifest_rows = list_manifest(db_path=db_path)
    counts_by_status: dict[str, int] = {}
    for row in manifest_rows:
        counts_by_status[row["status"]] = counts_by_status.get(row["status"], 0) + 1

    skipped = [row for row in manifest_rows if row["status"] == "skipped"]
    failed = [row for row in manifest_rows if row["status"] == "failed"]
    pending = [row for row in manifest_rows if row["status"] in {"discovered", "parsed"}]
    parsed = [row for row in manifest_rows if row["status"] in {"parsed", "evidence_extracted"}]
    artifacts = _artifact_records(vault)
    sensitive = [item for item in artifacts if str(item.get("sensitivity") or "") in {"high", "restricted", "sealed"}]
    duplicate_hashes = _duplicate_hashes(manifest_rows)
    jobs = _ingest_jobs(db_path)

    return {
        "counts_by_status": counts_by_status,
        "discovered_artifacts": [row for row in manifest_rows if row["status"] == "discovered"],
        "parsed_artifacts": parsed,
        "failed_parses": failed,
        "skipped_files": skipped,
        "sensitive_artifacts": sensitive,
        "duplicate_hashes": duplicate_hashes,
        "artifacts_awaiting_extraction": pending,
        "queued_extraction_jobs": [job for job in jobs if job["status"] == "queued"],
        "failed_extraction_jobs": [job for job in jobs if job["status"] == "failed"],
        "retry_wait_extraction_jobs": [job for job in jobs if job["status"] == "retry_wait"],
    }


def format_ingest_audit(report: dict[str, Any]) -> str:
    lines = ["Ingestion Audit", ""]
    lines.append("Artifacts by status:")
    counts = report.get("counts_by_status", {})
    if counts:
        for status in sorted(counts):
            lines.append(f"- {status}: {counts[status]}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Skipped files:")
    skipped = report.get("skipped_files", [])
    if skipped:
        for row in skipped:
            lines.append(f"- {row['source_path']} | {row.get('error') or 'no reason recorded'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Failed parses:")
    failed = report.get("failed_parses", [])
    if failed:
        for row in failed:
            lines.append(f"- {row['source_path']} | {row.get('error') or 'no error text'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Artifacts awaiting extraction:")
    pending = report.get("artifacts_awaiting_extraction", [])
    if pending:
        for row in pending:
            lines.append(f"- {row['source_path']} | {row['status']} | {row.get('artifact_id') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("High/restricted/sealed artifacts:")
    sensitive = report.get("sensitive_artifacts", [])
    if sensitive:
        for row in sensitive:
            lines.append(f"- {row['path']} | {row.get('sensitivity') or 'unknown'} | {row.get('summary') or ''}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Duplicate hashes:")
    duplicates = report.get("duplicate_hashes", [])
    if duplicates:
        for row in duplicates:
            lines.append(f"- {row['artifact_hash']} | {', '.join(row['source_paths'])}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Queued extraction jobs:")
    queued = report.get("queued_extraction_jobs", [])
    if queued:
        for job in queued:
            lines.append(f"- {job['id']} | {job['job_type']} | {job.get('error') or 'queued'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Failed extraction jobs:")
    failed_jobs = report.get("failed_extraction_jobs", [])
    if failed_jobs:
        for job in failed_jobs:
            lines.append(f"- {job['id']} | {job['job_type']} | {job.get('error') or 'no error text'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Retry-wait extraction jobs:")
    retry_jobs = report.get("retry_wait_extraction_jobs", [])
    if retry_jobs:
        for job in retry_jobs:
            lines.append(f"- {job['id']} | {job['job_type']} | next={job.get('scheduled_for') or '-'}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def format_ingest_status(report: dict[str, Any]) -> str:
    lines = ["Ingestion Status", ""]
    counts = report.get("counts_by_status", {})
    if counts:
        for status in sorted(counts):
            lines.append(f"- {status}: {counts[status]}")
    else:
        lines.append("- No artifacts discovered")
    lines.append("")
    pending = report.get("artifacts_awaiting_extraction", [])
    lines.append(f"artifacts_awaiting_extraction: {len(pending)}")
    lines.append(f"queued_jobs: {len(report.get('queued_extraction_jobs', []))}")
    lines.append(f"failed_jobs: {len(report.get('failed_extraction_jobs', []))}")
    return "\n".join(lines).rstrip() + "\n"


def format_ingest_batches(batches: list[dict[str, Any]]) -> str:
    lines = ["Ingestion Batches", ""]
    if not batches:
        lines.append("- None")
        return "\n".join(lines).rstrip() + "\n"
    for batch in batches:
        summary = batch.get("summary") or {}
        lines.append(
            f"- {batch.get('id') or '-'} | {batch.get('status') or '-'} | mode={batch.get('mode') or '-'} | "
            f"source_root={batch.get('source_root') or '-'} | created={batch.get('created_at') or '-'} | "
            f"artifacts={summary.get('artifacts', 0) if isinstance(summary, dict) else 0} | jobs={summary.get('jobs', 0) if isinstance(summary, dict) else 0}"
        )
    return "\n".join(lines).rstrip() + "\n"


def format_ingest_batch_summary(batch_report: dict[str, Any] | None) -> str:
    if not batch_report:
        return "Batch not found.\n"
    batch = batch_report.get("batch") or {}
    summary = batch_report.get("summary") or {}
    lines = ["Ingestion Batch", ""]
    lines.append(f"Batch ID: {batch.get('id') or '-'}")
    lines.append(f"Source root: {batch.get('source_root') or '-'}")
    lines.append(f"Mode: {batch.get('mode') or '-'}")
    lines.append(f"Status: {batch.get('status') or '-'}")
    lines.append(f"Created: {batch.get('created_at') or '-'}")
    lines.append(f"Started: {batch.get('started_at') or '-'}")
    lines.append(f"Finished: {batch.get('finished_at') or '-'}")
    lines.append(f"Requested by: {batch.get('requested_by') or '-'}")
    if batch.get("error"):
        lines.append(f"Error: {batch.get('error')}")
    if batch.get("notes"):
        lines.append(f"Notes: {batch.get('notes')}")
    lines.append("")
    lines.append("Summary:")
    if isinstance(summary, dict):
        for key in sorted(summary):
            lines.append(f"- {key}: {summary[key]}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Artifacts:")
    artifacts = batch_report.get("artifacts", [])
    if artifacts:
        for row in artifacts:
            lines.append(f"- {row.get('id') or '-'} | {row.get('status') or '-'} | {row.get('path') or row.get('source_path') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Jobs:")
    jobs = batch_report.get("jobs", [])
    if jobs:
        for job in jobs:
            lines.append(f"- {job.get('id') or '-'} | {job.get('job_type') or '-'} | {job.get('status') or '-'}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Skipped files:")
    skipped = batch_report.get("skipped_files", [])
    if skipped:
        for row in skipped:
            lines.append(f"- {row.get('source_path') or '-'} | {row.get('error') or 'no reason recorded'}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def format_ingest_batch_audit(batch_report: dict[str, Any] | None) -> str:
    return format_ingest_batch_summary(batch_report)


def format_ingest_plan(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = ["Ingestion Preview", ""]
    lines.append(f"Path: {report.get('path') or '-'}")
    if report.get("proposed_batch_summary"):
        batch = report["proposed_batch_summary"]
        lines.append(f"Proposed batch mode: {batch.get('mode') or '-'}")
        lines.append(f"Would create batch: {report.get('would_create_batch', False)}")
        lines.append(f"Batch source root: {batch.get('source_root') or '-'}")
        if batch.get("options") is not None:
            lines.append(f"Batch options: {json.dumps(batch.get('options'), ensure_ascii=True, sort_keys=True)}")
        lines.append("")
    lines.append(f"Total files seen: {summary.get('total_files_seen', 0)}")
    lines.append(f"Would ingest: {summary.get('would_ingest', 0)}")
    lines.append(f"Would skip: {summary.get('would_skip', 0)}")
    lines.append(f"Would parse: {summary.get('would_parse', 0)}")
    lines.append(f"Would create artifact only: {summary.get('would_create_artifact_only', 0)}")
    lines.append(f"Would enqueue parse jobs: {summary.get('would_enqueue_parse_jobs', 0)}")
    lines.append(f"Would enqueue evidence-extraction jobs: {summary.get('would_enqueue_evidence_extraction_jobs', 0)}")
    lines.append(f"Changed files already known in manifest: {summary.get('changed_files_already_known_in_manifest', 0)}")
    lines.append(f"Unsupported file types: {summary.get('unsupported', 0)}")
    lines.append(f"Skipped secret-like files: {summary.get('skipped_secret_like_files', 0)}")
    lines.append(
        "Sensitivity counts: "
        f"high={summary.get('high_count', 0)}, "
        f"restricted={summary.get('restricted_count', 0)}, "
        f"sealed={summary.get('sealed_count', 0)}"
    )
    lines.append("")
    lines.append("Files:")
    files = report.get("files", [])
    if files:
        for item in files:
            planned = item.get("planned_action") or "-"
            classification = item.get("classification") or "-"
            queue = ", ".join(item.get("would_enqueue") or []) or "-"
            lines.append(
                f"- {item.get('source_path') or '-'} | {classification} | {planned} | "
                f"manifest_state={item.get('manifest_state') or '-'} | "
                f"sensitivity={item.get('sensitivity') or '-'} | source_type={item.get('source_type') or '-'} | "
                f"hash={item.get('artifact_hash') or '-'} | known_artifact_id={item.get('known_artifact_id') or '-'} | "
                f"would_enqueue={queue}"
            )
            if item.get("skip_reason"):
                lines.append(f"  - skip_reason: {item.get('skip_reason')}")
            if item.get("duplicate_hash"):
                lines.append("  - duplicate_hash: true")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("Duplicate hashes:")
    duplicates = report.get("duplicate_hashes", [])
    if duplicates:
        for row in duplicates:
            lines.append(f"- {row['artifact_hash']} | {', '.join(row['source_paths'])}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _scan_batch_options(
    path: Path,
    *,
    vault: Path,
    db_path: Path,
    max_file_size_bytes: int,
    include_ext: list[str] | None,
    exclude_ext: list[str] | None,
    include_hidden: bool,
    allow_restricted: bool,
    allow_high: bool,
    allow_sealed: bool,
    batch_mode: str,
) -> dict[str, Any]:
    return {
        "source_root": str(path.resolve()),
        "vault": str(vault),
        "db_path": str(db_path),
        "max_file_size_bytes": int(max_file_size_bytes),
        "include_ext": include_ext or [],
        "exclude_ext": exclude_ext or [],
        "include_hidden": bool(include_hidden),
        "allow_restricted": bool(allow_restricted),
        "allow_high": bool(allow_high),
        "allow_sealed": bool(allow_sealed),
        "mode": batch_mode,
    }


def _plan_ingest_file(
    source_path: Path,
    *,
    vault: Path,
    db_path: Path,
    manifest_snapshot: list[dict[str, Any]],
    max_file_size_bytes: int,
    skip_if_inside_vault: bool,
    include_hidden: bool,
    allow_restricted: bool,
    allow_high: bool,
    allow_sealed: bool,
    filters: dict[str, set[str]],
) -> dict[str, Any]:
    source_path = source_path.resolve()
    source_path_str = str(source_path)
    plan: dict[str, Any] = {
        "source_path": source_path_str,
        "file_name": source_path.name,
        "file_ext": source_path.suffix.lower(),
        "planned_action": "skip",
        "classification": "skipped",
        "manifest_state": None,
        "skip_reason": None,
        "would_enqueue": [],
        "known_artifact_id": None,
        "manifest_status": None,
        "manifest_error": None,
        "artifact_hash": None,
        "source_type": None,
        "sensitivity": None,
        "size_bytes": None,
        "duplicate_hash": False,
        "will_parse": False,
    }
    if not source_path.exists() or not source_path.is_file():
        plan["skip_reason"] = "Not a file"
        return plan
    if skip_if_inside_vault and _is_inside_vault(source_path, vault):
        plan["skip_reason"] = "Source is inside the vault"
        return plan
    if not include_hidden and _is_hidden_path(source_path):
        plan["skip_reason"] = "Hidden files are excluded by default"
        return plan
    if filters["include_ext"] and plan["file_ext"] not in filters["include_ext"]:
        plan["skip_reason"] = "Extension not included"
        return plan
    if filters["exclude_ext"] and plan["file_ext"] in filters["exclude_ext"]:
        plan["skip_reason"] = "Extension excluded"
        return plan
    excluded_reason = _exclusion_reason(source_path)
    if excluded_reason:
        plan["skip_reason"] = excluded_reason
        return plan
    stat = source_path.stat()
    plan["size_bytes"] = stat.st_size
    if stat.st_size > max_file_size_bytes:
        plan["skip_reason"] = f"File exceeds size limit ({stat.st_size} bytes > {max_file_size_bytes} bytes)"
        return plan
    source_type, supported = _classify_source_type(source_path)
    plan["source_type"] = source_type
    artifact_hash = _hash_file(source_path)
    plan["artifact_hash"] = artifact_hash
    sensitivity = _classify_sensitivity(source_path, source_type, preview_path=source_path)
    plan["sensitivity"] = sensitivity
    if sensitivity == "sealed" and not allow_sealed:
        plan["skip_reason"] = "Sealed content is excluded from default ingestion"
        return plan
    if sensitivity == "restricted" and not allow_restricted:
        plan["skip_reason"] = "Restricted content requires explicit allow-restricted"
        return plan
    if sensitivity == "high" and not allow_high:
        plan["skip_reason"] = "High sensitivity content requires explicit allow-high"
        return plan
    manifest_index = _build_manifest_index(manifest_snapshot)
    manifest_row = manifest_index["by_source"].get(source_path_str)
    if manifest_row:
        plan["known_artifact_id"] = manifest_row.get("artifact_id")
        plan["manifest_status"] = manifest_row.get("status")
        plan["manifest_error"] = manifest_row.get("error")
    duplicate_rows = [row for row in manifest_index["by_hash"].get(artifact_hash, []) if str(row.get("source_path") or "") != source_path_str]
    if duplicate_rows:
        plan["duplicate_hash"] = True
    if manifest_row and str(manifest_row.get("artifact_hash") or "") == artifact_hash:
        plan["manifest_state"] = "unchanged"
        plan["classification"] = "duplicate_hash" if duplicate_rows else "unchanged"
        if plan["manifest_status"] not in {"evidence_extracted", "skipped"} and supported:
            plan["planned_action"] = "reuse_existing_pending_parse"
            plan["will_parse"] = True
            plan["would_enqueue"] = ["ingest.parse_file", "ingest.extract_evidence", "ingest.reindex_artifact"]
        else:
            plan["planned_action"] = "noop"
        return plan
    if manifest_row and str(manifest_row.get("artifact_hash") or "") != artifact_hash:
        plan["manifest_state"] = "changed"
    else:
        plan["manifest_state"] = "new"
    if not supported:
        plan["classification"] = "unsupported"
        plan["planned_action"] = "create_artifact_only"
        return plan
    if duplicate_rows:
        plan["classification"] = "duplicate_hash"
    else:
        plan["classification"] = plan["manifest_state"] or "new"
    plan["planned_action"] = "create_artifact_and_parse"
    plan["will_parse"] = True
    plan["would_enqueue"] = ["ingest.parse_file", "ingest.extract_evidence", "ingest.reindex_artifact"]
    return plan


def _ingest_single_file(
    source_path: Path,
    *,
    vault: Path,
    db_path: Path,
    conn: sqlite3.Connection,
    queue_jobs: bool,
    max_file_size_bytes: int,
    skip_if_inside_vault: bool,
    include_hidden: bool,
    allow_restricted: bool,
    allow_high: bool,
    allow_sealed: bool,
    filters: dict[str, set[str]],
    batch_id: str | None = None,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    source_path_str = str(source_path)
    now = _iso_now()
    if not source_path.exists() or not source_path.is_file():
        _upsert_manifest(conn, source_path_str, status="skipped", error="Not a file", batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": "Not a file", "batch_id": batch_id}
    if skip_if_inside_vault and _is_inside_vault(source_path, vault):
        _upsert_manifest(conn, source_path_str, status="skipped", error="Source is inside the vault", batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": "Source is inside the vault", "batch_id": batch_id}
    if not include_hidden and _is_hidden_path(source_path):
        _upsert_manifest(conn, source_path_str, status="skipped", error="Hidden files are excluded by default", batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": "Hidden files are excluded by default", "batch_id": batch_id}
    if filters["include_ext"] and source_path.suffix.lower() not in filters["include_ext"]:
        _upsert_manifest(conn, source_path_str, status="skipped", error="Extension not included", batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": "Extension not included", "batch_id": batch_id}
    if filters["exclude_ext"] and source_path.suffix.lower() in filters["exclude_ext"]:
        _upsert_manifest(conn, source_path_str, status="skipped", error="Extension excluded", batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": "Extension excluded", "batch_id": batch_id}
    excluded_reason = _exclusion_reason(source_path)
    if excluded_reason:
        _upsert_manifest(conn, source_path_str, status="skipped", error=excluded_reason, batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": excluded_reason, "batch_id": batch_id}

    stat = source_path.stat()
    if stat.st_size > max_file_size_bytes:
        reason = f"File exceeds size limit ({stat.st_size} bytes > {max_file_size_bytes} bytes)"
        _upsert_manifest(conn, source_path_str, status="skipped", error=reason, batch_id=batch_id)
        return {"source_path": source_path_str, "status": "skipped", "error": reason, "batch_id": batch_id}

    source_type, supported = _classify_source_type(source_path)
    file_name = source_path.name
    file_ext = source_path.suffix.lower()
    mime_type = mimetypes.guess_type(file_name)[0]
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    imported_at = now
    artifact_hash = _hash_file(source_path)
    sensitivity = _classify_sensitivity(source_path, source_type, preview_path=source_path)
    if sensitivity == "sealed":
        reason = "Sealed content is excluded from default ingestion"
        _upsert_manifest(conn, source_path_str, artifact_hash=artifact_hash, last_seen=now, last_ingested=None, status="skipped", error=reason, batch_id=batch_id)
        conn.commit()
        return {
            "source_path": source_path_str,
            "status": "skipped",
            "error": reason,
            "artifact_hash": artifact_hash,
            "artifact_id": None,
            "batch_id": batch_id,
        }
    if sensitivity == "restricted" and not allow_restricted:
        reason = "Restricted content requires explicit allow-restricted"
        _upsert_manifest(conn, source_path_str, artifact_hash=artifact_hash, last_seen=now, last_ingested=None, status="skipped", error=reason, batch_id=batch_id)
        conn.commit()
        return {"source_path": source_path_str, "status": "skipped", "error": reason, "artifact_hash": artifact_hash, "artifact_id": None, "batch_id": batch_id}
    if sensitivity == "high" and not allow_high:
        reason = "High sensitivity content requires explicit allow-high"
        _upsert_manifest(conn, source_path_str, artifact_hash=artifact_hash, last_seen=now, last_ingested=None, status="skipped", error=reason, batch_id=batch_id)
        conn.commit()
        return {"source_path": source_path_str, "status": "skipped", "error": reason, "artifact_hash": artifact_hash, "artifact_id": None, "batch_id": batch_id}
    manifest = _manifest_by_source_path(conn, source_path_str)
    existing_hash = str(manifest.get("artifact_hash") or "")
    existing_status = str(manifest.get("status") or "")
    artifact_id = str(manifest.get("artifact_id") or "") or None
    queue_parse = False

    if artifact_hash != existing_hash or not artifact_id:
        artifact_id = _artifact_id(source_path_str, artifact_hash)
        artifact_path = _artifact_record_path(vault, artifact_id, source_path_str, artifact_hash)
        if not artifact_path.exists():
            summary = _summarize_text(_read_text_source(source_path) if supported else _preview_text(source_path), file_name=file_name)
            artifact_record = new_artifact(
                vault=vault,
                source_path=source_path_str,
                source_type=source_type,
                artifact_hash=artifact_hash,
                file_name=file_name,
                file_ext=file_ext,
                imported_at=imported_at,
                modified_at=modified_at,
                size_bytes=stat.st_size,
                sensitivity=sensitivity,
                compartments=_classify_compartments(source_path_str, ""),
                arena=_infer_arena(source_path_str, file_name, ""),
                source_uri=f"file://{source_path_str}",
                mime_type=mime_type,
                summary=summary,
                extracted_text_ref=_extracted_text_path(vault, artifact_id),
                ingestion_status="discovered" if supported else "skipped",
                parse_errors=[] if supported else ["Unsupported file type for parsing"],
                batch_id=batch_id,
            )
            artifact_id = str(load_markdown(artifact_record.path).frontmatter.get("id"))
        queue_parse = supported
        _upsert_manifest(
            conn,
            source_path_str,
            artifact_hash=artifact_hash,
            last_seen=now,
            last_ingested=None,
            status="discovered" if supported else "skipped",
            artifact_id=artifact_id,
            error=None if supported else "Unsupported file type for parsing",
            batch_id=batch_id,
        )
        result = {
            "source_path": source_path_str,
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "status": "discovered" if supported else "skipped",
            "error": None if supported else "Unsupported file type for parsing",
            "batch_id": batch_id,
        }
    else:
        _upsert_manifest(conn, source_path_str, artifact_hash=artifact_hash, last_seen=now, artifact_id=artifact_id, status=existing_status or "discovered", error=manifest.get("error"), batch_id=batch_id)
        result = {
            "source_path": source_path_str,
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "status": existing_status or "discovered",
            "error": manifest.get("error"),
            "batch_id": batch_id,
        }
        queue_parse = supported and existing_status not in {"evidence_extracted", "skipped"}

    conn.commit()
    if queue_jobs and queue_parse and artifact_id:
        from .jobs import enqueue_job

        job_id = enqueue_job(
            "ingest.parse_file",
            {
                "vault": str(vault),
                "db_path": str(db_path),
                "artifact_id": artifact_id,
                "source_path": source_path_str,
                "artifact_hash": artifact_hash,
                "file_name": file_name,
                "file_ext": file_ext,
                "source_type": source_type,
                "source_uri": f"file://{source_path_str}",
                "imported_at": imported_at,
                "modified_at": modified_at,
                "size_bytes": stat.st_size,
                "sensitivity": sensitivity,
                "batch_id": batch_id,
            },
            priority=None,
            batch_id=batch_id,
            db_path=db_path,
        )
        result.setdefault("queued_jobs", []).append({"job_type": "ingest.parse_file", "job_id": job_id})
        result.setdefault("queued_jobs", [])[-1]["batch_id"] = batch_id
    return result


def _upsert_manifest(
    conn: sqlite3.Connection,
    source_path: str,
    *,
    artifact_hash: str | None = None,
    last_seen: str | None = None,
    last_ingested: str | None = None,
    status: str,
    artifact_id: str | None = None,
    batch_id: str | None = None,
    error: str | None = None,
) -> None:
    ensure_ingestion_manifest_table(conn)
    existing = conn.execute("SELECT source_path FROM ingestion_manifest WHERE source_path = ?", (source_path,)).fetchone()
    if existing:
        assignments: list[str] = ["status = ?"]
        values: list[Any] = [status]
        if artifact_hash is not None:
            assignments.append("artifact_hash = COALESCE(?, artifact_hash)")
            values.append(artifact_hash)
        if last_seen is not None:
            assignments.append("last_seen = COALESCE(?, last_seen)")
            values.append(last_seen)
        if last_ingested is not None:
            assignments.append("last_ingested = COALESCE(?, last_ingested)")
            values.append(last_ingested)
        if artifact_id is not None:
            assignments.append("artifact_id = COALESCE(?, artifact_id)")
            values.append(artifact_id)
        if batch_id is not None:
            assignments.append("batch_id = COALESCE(?, batch_id)")
            values.append(batch_id)
        if error is not None:
            assignments.append("error = ?")
            values.append(error)
        elif status not in {"skipped", "failed"}:
            assignments.append("error = NULL")
        conn.execute(f"UPDATE ingestion_manifest SET {', '.join(assignments)} WHERE source_path = ?", (*values, source_path))
    else:
        conn.execute(
            """
            INSERT INTO ingestion_manifest (source_path, artifact_hash, last_seen, last_ingested, status, artifact_id, batch_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source_path, artifact_hash, last_seen, last_ingested, status, artifact_id, batch_id, error),
        )


def _update_manifest(
    source_path: str,
    *,
    db_path: Path | None = None,
    artifact_hash: str | None = None,
    last_seen: str | None = None,
    last_ingested: str | None = None,
    status: str,
    artifact_id: str | None = None,
    batch_id: str | None = None,
    error: str | None = None,
) -> None:
    conn = _connect(db_path)
    try:
        ensure_ingestion_manifest_table(conn)
        _upsert_manifest(
            conn,
            source_path,
            artifact_hash=artifact_hash,
            last_seen=last_seen,
            last_ingested=last_ingested,
            status=status,
            artifact_id=artifact_id,
            batch_id=batch_id,
            error=error,
        )
        conn.commit()
    finally:
        conn.close()


def _manifest_by_source_path(conn: sqlite3.Connection, source_path: str) -> dict[str, Any]:
    ensure_ingestion_manifest_table(conn)
    row = conn.execute("SELECT * FROM ingestion_manifest WHERE source_path = ?", (source_path,)).fetchone()
    return dict(row) if row else {}


def _manifest_by_artifact_id(db_path: Path | None, artifact_id: str) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        ensure_ingestion_manifest_table(conn)
        row = conn.execute("SELECT * FROM ingestion_manifest WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_record_id(path: Path) -> str:
    return str(load_markdown(path).frontmatter.get("id", path.stem))


def _ensure_evidence_record(
    *,
    vault: Path,
    title: str,
    record_date: str,
    source_type: str,
    source_uri: str | None,
    artifact_ref: str | None,
    artifact_hash: str | None,
    timestamp_of_artifact: str | None,
    actors: list[str],
    arena: str,
    compartments: list[str],
    sensitivity: str,
    reliability: str,
    privacy: str,
    significance: str,
    summary: str,
    observed_facts: list[str],
    verbatim_excerpt: str | None,
    linked_claims: list[str],
    linked_episodes: list[str],
    batch_id: str | None = None,
) -> Path:
    record_path = vault / "evidence" / "records" / f"{record_date}-{slugify(title)}.md"
    if record_path.exists():
        return record_path
    record = new_evidence(
        vault,
        title,
        record_date=record_date,
        source_type=source_type,
        source_uri=source_uri,
        artifact_ref=artifact_ref,
        artifact_hash=artifact_hash,
        timestamp_of_artifact=timestamp_of_artifact,
        actors=actors,
        arena=arena,
        compartments=compartments,
        sensitivity=sensitivity,
        reliability=reliability,
        privacy=privacy,
        significance=significance,
        summary=summary,
        observed_facts=observed_facts,
        verbatim_excerpt=verbatim_excerpt,
        linked_claims=linked_claims,
        linked_episodes=linked_episodes,
        batch_id=batch_id,
    )
    return record.path


def _ensure_claim_record(
    *,
    vault: Path,
    claim_text: str,
    record_date: str,
    claim_class: str,
    owner: str,
    status: str,
    confidence: float,
    supporting_evidence: list[str],
    contradicting_evidence: list[str],
    linked_patterns: list[str],
    first_seen: str,
    last_reviewed: str,
    review_notes: str,
    source_type: str | None,
    source_uri: str | None,
    artifact_ref: str | None,
    artifact_hash: str | None,
    timestamp_of_artifact: str | None,
    arena: str,
    compartments: list[str],
    privacy: str,
    significance: str,
    summary: str,
    batch_id: str | None = None,
) -> Path:
    safe_slug = slugify(claim_text)[:80]
    record_path = vault / "claims" / f"{record_date}-{safe_slug}.md"
    if record_path.exists():
        return record_path
    record = new_claim(
        vault,
        claim_text,
        record_date=record_date,
        claim_class=claim_class,
        owner=owner,
        status=status,
        confidence=confidence,
        supporting_evidence=supporting_evidence,
        contradicting_evidence=contradicting_evidence,
        linked_patterns=linked_patterns,
        first_seen=first_seen,
        last_reviewed=last_reviewed,
        review_notes=review_notes,
        source_type=source_type,
        source_uri=source_uri,
        artifact_ref=artifact_ref,
        artifact_hash=artifact_hash,
        timestamp_of_artifact=timestamp_of_artifact,
        arena=arena,
        compartments=compartments,
        privacy=privacy,
        significance=significance,
        summary=summary,
        batch_id=batch_id,
    )
    return record.path


def _artifact_id(source_path: str, artifact_hash: str) -> str:
    source_hash = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:8]
    hash_hex = artifact_hash.replace("sha256:", "")
    return f"artifact.{slugify(Path(source_path).stem or 'artifact')}.{source_hash}.{hash_hex[:8]}"


def _artifact_record_path(vault: Path, artifact_id: str, source_path: str, artifact_hash: str) -> Path:
    file_stem = slugify(Path(source_path).stem or "artifact")
    source_hash = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:8]
    hash_hex = artifact_hash.replace("sha256:", "")
    return vault / "evidence" / "artifacts" / f"{file_stem}-{source_hash}-{hash_hex[:8]}.md"


def _extracted_text_path(vault: Path, artifact_id: str) -> str:
    return str(vault / "evidence" / "artifacts" / "extracted" / f"{artifact_id}.txt")


def _walk_files(root: Path, include_hidden: bool = False) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for current_root, dirs, filenames in os.walk(root):
        if not include_hidden:
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]
        else:
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for filename in filenames:
            if not include_hidden and filename.startswith("."):
                continue
            path = Path(current_root) / filename
            files.append(path)
    return sorted(files)


def _is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts if part not in {".", ".."})


def _is_inside_vault(path: Path, vault: Path) -> bool:
    try:
        return path.resolve().is_relative_to(vault.resolve())
    except AttributeError:
        try:
            path.resolve().relative_to(vault.resolve())
            return True
        except Exception:
            return False
    except Exception:
        return False


def _exclusion_reason(path: Path) -> str | None:
    lower_name = path.name.lower()
    if path.name in EXCLUDED_NAME_EXACT:
        return "Excluded secret or credential file"
    if any(lower_name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return "Excluded secret or credential file"
    if any(part.lower() in {"password-store", "passwords", "secrets", "credentials"} for part in path.parts):
        return "Excluded secret or credential store"
    if lower_name.startswith(".env"):
        return "Excluded secret or credential file"
    return None


def _classify_source_type(path: Path) -> tuple[str, bool]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown", True
    if suffix in {".txt", ".json", ".csv"}:
        return "text", True
    if suffix == ".pdf":
        return "pdf", False
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        return "image", False
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime.startswith("text/") or mime in {"application/json", "text/csv"}:
        return "text", True
    if mime.startswith("image/"):
        return "image", False
    if mime == "application/pdf":
        return "pdf", False
    return "other", False


def _classify_sensitivity(path: Path | str, source_type: str, preview_path: Path | None = None) -> str:
    text = f"{path} {source_type}".lower()
    if preview_path and preview_path.exists() and preview_path.is_file():
        try:
            preview = _preview_text(preview_path)
            text += " " + preview.lower()
        except Exception:
            pass
    if any(keyword in text for keyword in SECRET_KEYWORDS):
        return "sealed"
    if any(keyword in text for keyword in FINANCIAL_KEYWORDS):
        return "high"
    if any(keyword in text for keyword in LEGAL_KEYWORDS):
        return "restricted"
    if any(keyword in text for keyword in HEALTH_KEYWORDS):
        return "restricted"
    if any(keyword in text for keyword in WORK_KEYWORDS):
        return "high"
    if source_type in {"markdown", "text"}:
        return "medium"
    return "low"


def _classify_compartments(path: str, text: str) -> list[str]:
    lowered = f"{path} {text}".lower()
    compartments: list[str] = []
    for compartment, keywords in {
        "legal": LEGAL_KEYWORDS,
        "health": HEALTH_KEYWORDS,
        "financial": FINANCIAL_KEYWORDS,
    }.items():
        if any(keyword in lowered for keyword in keywords):
            compartments.append(compartment)
    return compartments


def _privacy_for_sensitivity(sensitivity: str) -> str:
    if sensitivity == "sealed":
        return "sealed"
    if sensitivity in {"high", "restricted"}:
        return "personal_sensitive"
    return "personal"


def _reliability_for_sensitivity(sensitivity: str) -> str:
    if sensitivity in {"high", "restricted", "sealed"}:
        return "high"
    return "medium"


def _infer_arena(path: str, file_name: str, text: str) -> str:
    lowered = f"{path} {file_name} {text}".lower()
    if any(keyword in lowered for keyword in WORK_KEYWORDS):
        return "work"
    if any(keyword in lowered for keyword in FINANCIAL_KEYWORDS):
        return "financial"
    if any(keyword in lowered for keyword in LEGAL_KEYWORDS):
        return "status"
    return "cross_arena"


def _preview_text(path: Path, max_chars: int | None = None) -> str:
    max_chars = int(max_chars or load_config().get("ingest", {}).get("text_preview_chars", 4000))
    raw = path.read_bytes()[: max_chars * 2]
    return raw.decode("utf-8", errors="ignore")


def _read_text_source(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() in {".md", ".markdown"}:
        try:
            doc = load_markdown(path)
            if doc.body.strip():
                return doc.body
        except FrontmatterError:
            pass
    return text


def _summarize_text(text: str, file_name: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return file_name
    for line in lines:
        if len(line) <= 180:
            return line
    return lines[0][:180]


def _extract_fact_and_claim_lines(text: str) -> tuple[list[str], list[str]]:
    facts: list[str] = []
    claims: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        lowered = f" {cleaned.lower()} "
        if any(marker in lowered for marker in FACT_MARKERS) or (":" in cleaned and len(cleaned) < 220):
            facts.append(cleaned)
        if any(marker in lowered for marker in CLAIM_MARKERS):
            claims.append(cleaned)
    if not facts and text.strip():
        facts.append(_summarize_text(text, file_name="document"))
    return facts[:10], claims[:10]


def _clean_line(line: str) -> str:
    cleaned = line.strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^[\-\*\u2022]\s*", "", cleaned)
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
    return cleaned.strip()


def _claim_class_for_text(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("always", "never", "i am ", "i'm ", "identity", "person")):
        return "identity_claim"
    if any(token in lowered for token in ("feel", "seems", "appears", "maybe", "might", "probably", "likely")):
        return "interpretation"
    if any(token in lowered for token in ("should", "need to", "must", "have to", "ought")):
        return "value_statement"
    if any(token in lowered for token in ("will ", "next ", "tomorrow", "soon", "expect")):
        return "prediction"
    return "inference"


def _owner_for_text(source_path: str, text: str) -> str:
    lowered = f"{source_path} {text}".lower()
    if "journal" in lowered or "diary" in lowered or re.search(r"\bi\b", lowered):
        return "user"
    return "external_actor"


def _claim_confidence_for_text(text: str, sensitivity: str) -> float:
    base = 0.5
    if any(token in text.lower() for token in ("always", "never", "must", "only")):
        base -= 0.1
    if sensitivity in {"high", "restricted"}:
        base += 0.1
    return max(0.05, min(0.95, base))


def _duplicate_hashes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        artifact_hash = str(row.get("artifact_hash") or "")
        if not artifact_hash:
            continue
        grouped.setdefault(artifact_hash, []).append(str(row.get("source_path") or ""))
    return [
        {"artifact_hash": artifact_hash, "source_paths": sorted(set(paths))}
        for artifact_hash, paths in grouped.items()
        if len(set(paths)) > 1
    ]


def _ingest_jobs(db_path: Path) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE job_type LIKE 'ingest.%'
            ORDER BY created_at DESC, priority ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _artifact_records(vault: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = vault / "evidence" / "artifacts"
    if not root.exists():
        return records
    for path in sorted(root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type")) != "artifact":
            continue
        records.append({**doc.frontmatter, "path": str(path.relative_to(vault))})
    return records


def _find_artifact_path(vault: Path, artifact_id: str) -> Path | None:
    root = vault / "evidence" / "artifacts"
    if not root.exists():
        return None
    for path in sorted(root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("id")) == artifact_id:
            return path
    return None


def _update_artifact_record(vault: Path, artifact_id: str, updates: dict[str, Any]) -> None:
    artifact_path = _find_artifact_path(vault, artifact_id)
    if artifact_path is None:
        return
    try:
        doc = load_markdown(artifact_path)
    except Exception:
        return
    frontmatter = dict(doc.frontmatter)
    frontmatter.update(updates)
    if "linked_evidence" in frontmatter and frontmatter["linked_evidence"] is None:
        frontmatter["linked_evidence"] = []
    if "linked_claims" in frontmatter and frontmatter["linked_claims"] is None:
        frontmatter["linked_claims"] = []
    if "parse_errors" in frontmatter and frontmatter["parse_errors"] is None:
        frontmatter["parse_errors"] = []
    write_markdown(artifact_path, frontmatter, doc.body)


def _json_loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    return conn


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")



def ingest_reference_sources(
    sources: list[Path],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    replace: bool = False,
    on_exists: str | None = None,
    link_entities: list[str] | None = None,
    plan_only: bool = False,
) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    existence_policy = "replace" if replace else (str(on_exists or "abort").strip().lower() or "abort")
    if existence_policy not in {"abort", "replace", "merge"}:
        raise ValueError("on_exists must be one of: abort, replace, merge")
    if existence_policy == "merge":
        raise NotImplementedError("Reference merge re-ingestion is not implemented yet; use --on-exists replace or abort.")
    link_entities = [str(item).strip() for item in (link_entities or []) if str(item).strip()]

    entity_catalog = _load_entity_catalog(vault)
    linked_entity_ids = _resolve_explicit_entity_links(link_entities, entity_catalog, vault)
    plans: list[dict[str, Any]] = []
    created_records: list[dict[str, Any]] = []
    replaced_files: list[str] = []
    warnings: list[str] = []
    total_chunks = 0

    for source in sources:
        source = source.resolve()
        if source.is_dir():
            child_sources = _reference_files_in_directory(source)
            nested = ingest_reference_sources(
                child_sources,
                vault=vault,
                db_path=db_path,
                replace=replace,
                on_exists=existence_policy,
                link_entities=link_entities,
                plan_only=plan_only,
            )
            plans.extend(nested.get("documents", []))
            created_records.extend(nested.get("created_records", []))
            replaced_files.extend(nested.get("replaced_files", []))
            warnings.extend(nested.get("warnings", []))
            total_chunks += int(nested.get("total_chunks", 0))
            continue

        document = _load_reference_document(source)
        document_title = document["title"]
        source_document = document_title
        source_locator = document["source_locator"]
        chunks = chunk_document(
            document["text"],
            document_title,
            mode="auto",
            source_ref_base=source_locator,
        )
        if existence_policy == "replace":
            removed = _remove_existing_reference_chunks(vault, source_document, plan_only=plan_only)
            replaced_files.extend(removed)
        elif _has_existing_reference_chunks(vault, source_document):
            raise FileExistsError(
                f"Reference document already ingested: {source_document}. "
                "Re-run with --on-exists replace to replace the old chunks, "
                "or --on-exists abort to keep the existing version."
            )

        doc_plan = {
            "source_path": str(source),
            "source_document": source_document,
            "source_type": document["source_type"],
            "chunk_count": len(chunks),
            "linked_entity_ids": sorted(linked_entity_ids),
            "warnings": list(document["warnings"]),
        }
        plans.append(doc_plan)
        warnings.extend(document["warnings"])
        total_chunks += len(chunks)

        if plan_only:
            detected_entities: list[dict[str, Any]] = []
            for chunk in chunks:
                chunk_links, would_create = _link_entities_for_chunk_impl(
                    vault=vault,
                    chunk=chunk,
                    entity_catalog=entity_catalog,
                    explicit_links=linked_entity_ids,
                    create_entities=False,
                )
                detected_entities.extend(would_create)
                doc_plan.setdefault("detected_entity_ids", [])
                doc_plan["detected_entity_ids"].extend(chunk_links)
            if detected_entities:
                doc_plan["would_create_entities"] = detected_entities
            continue

        for chunk in chunks:
            chunk_links = set(linked_entity_ids)
            new_links, newly_created = _link_entities_for_chunk(
                vault=vault,
                chunk=chunk,
                entity_catalog=entity_catalog,
                explicit_links=linked_entity_ids,
            )
            chunk_links.update(new_links)
            for entity in newly_created:
                if entity.get("id"):
                    entity_catalog.append(entity)
            title = f"{document_title} - {chunk.title} [chunk {chunk.chunk_index + 1}]"
            record = new_knowledge(
                vault,
                title,
                    category=_knowledge_category_for_source(document["source_type"], source.suffix.lower()),
                domain_primary="cross_arena",
                domain_secondary=[],
                privacy="personal",
                disclosure="personal",
                significance="medium",
                summary=chunk.breadcrumb,
                links=sorted(chunk_links),
                confidence="high",
                confidence_basis="Authoritative program documentation",
                last_confirmed=_today_iso(),
                review_after=_one_year_from_today(),
                source_document=document_title,
                source_section=chunk.title,
                source_ref=chunk.source_ref,
                chunk_index=chunk.chunk_index,
                total_chunks=chunk.total_chunks,
                body=chunk.body,
            )
            created_records.append(
                {
                    "path": str(record.path),
                    "title": title,
                    "links": sorted(chunk_links),
                    "source_document": document_title,
                    "source_section": chunk.title,
                }
            )

    if not plan_only and (created_records or replaced_files):
        from .rebuild_index import rebuild_index

        rebuild_index(vault=vault, db_path=db_path)

    return {
        "plan_only": plan_only,
        "documents": plans,
        "created_records": created_records,
        "replaced_files": replaced_files,
        "warnings": warnings,
        "total_chunks": total_chunks,
        "linked_entity_ids": sorted(linked_entity_ids),
    }


def format_reference_ingest_plan(plan: dict[str, Any]) -> str:
    lines = ["Reference ingest plan", ""]
    lines.append(f"documents: {len(plan.get('documents', []))}")
    lines.append(f"chunks: {plan.get('total_chunks', 0)}")
    linked = plan.get("linked_entity_ids") or []
    lines.append(f"prelinked_entities: {len(linked)}")
    replaced = plan.get("replaced_files") or []
    if replaced:
        lines.append(f"would_replace: {len(replaced)}")
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    lines.append("")
    for document in plan.get("documents", []):
        lines.append(f"- {document.get('source_document')} ({document.get('chunk_count', 0)} chunks)")
        detected = document.get("detected_entity_ids") or []
        if detected:
            lines.append(f"  - detected_entity_links: {len(set(detected))}")
        if document.get("would_create_entities"):
            lines.append(f"  - would_create_entities: {len(document.get('would_create_entities', []))}")
        for warning in document.get("warnings", []):
            lines.append(f"  - warning: {warning}")
    return "\n".join(lines).rstrip() + "\n"


def _reference_files_in_directory(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".md", ".markdown", ".txt", ".json", ".csv", ".pdf"}:
            continue
        files.append(path)
    return files


def _load_reference_document(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    warnings: list[str] = []
    if suffix == ".pdf":
        text, pdf_warnings = _extract_pdf_text(path)
        warnings.extend(pdf_warnings)
        source_type = "pdf"
    elif suffix in {".md", ".markdown"}:
        doc = load_markdown(path)
        text = doc.body.strip() or path.read_text(encoding="utf-8", errors="ignore")
        source_type = "markdown"
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
        source_type = "text"
    title = _reference_document_title(path, text)
    source_locator = path.name
    return {
        "path": path,
        "text": text,
        "title": title,
        "source_locator": source_locator,
        "source_type": source_type,
        "warnings": warnings,
    }


def _reference_document_title(path: Path, text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if heading:
            return heading.group(1).strip()
        if len(line.split()) <= 12 and not line.endswith((".", ":", ";")):
            return line
        break
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.stem


def _extract_pdf_text(path: Path) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        import fitz  # type: ignore
    except Exception:
        raise RuntimeError("PDF ingestion requires pymupdf. Install `lisan[pdf]` or `pymupdf`.")

    doc = fitz.open(str(path))
    pages: list[str] = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        page_number = page_index + 1
        page_text = page.get_text("text").strip()
        if page_text:
            pages.append(f"--- Page {page_number} ---\n{page_text}")
    combined = "\n\n".join(pages).strip()
    if len(combined) < 100 and len(doc) > 1:
        warnings.append("This PDF appears image-based; extracted text is sparse and may need OCR.")
    return combined, warnings


def _knowledge_category_for_source(source_type: str, suffix: str) -> str:
    lowered = f"{source_type} {suffix}"
    if any(token in lowered for token in ("legal", ".pdf")):
        return "frameworks"
    if any(token in lowered for token in ("technical", ".md", ".txt")):
        return "frameworks"
    return "frameworks"


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _one_year_from_today() -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=365)).isoformat()


def _load_entity_catalog(vault: Path) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    root = vault / "entities"
    if not root.exists():
        return catalog
    for path in sorted(root.rglob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type")) != "entity":
            continue
        name = str(doc.frontmatter.get("canonical_name") or doc.frontmatter.get("name") or path.stem).strip()
        aliases = [str(alias).strip() for alias in doc.frontmatter.get("aliases") or [] if str(alias).strip()]
        catalog.append(
            {
                "id": str(doc.frontmatter.get("id") or ""),
                "name": name,
                "aliases": aliases,
                "kind": str(doc.frontmatter.get("kind") or doc.frontmatter.get("subtype") or "thing"),
                "path": str(path),
            }
        )
    return catalog


def _resolve_explicit_entity_links(link_entities: list[str], catalog: list[dict[str, Any]], vault: Path) -> list[str]:
    resolved: list[str] = []
    for value in link_entities:
        entity = _find_entity_by_label(value, catalog)
        if entity is not None:
            resolved.append(str(entity["id"]))
            continue
        created = _ensure_entity_record(vault, value, kind="organization", summary=f"{value} referenced in imported material.")
        catalog.append(created)
        resolved.append(str(created["id"]))
    return sorted(dict.fromkeys(resolved))


def _find_entity_by_label(label: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = _normalize_label(label)
    for entity in catalog:
        labels = [
            _normalize_label(entity.get("id")),
            _normalize_label(entity.get("name")),
            _normalize_label(slugify(str(entity.get("name") or ""))),
        ]
        labels.extend(_normalize_label(alias) for alias in entity.get("aliases", []))
        if needle and needle in labels:
            return entity
    return None


def _link_entities_for_chunk(
    *,
    vault: Path,
    chunk: Chunk,
    entity_catalog: list[dict[str, Any]],
    explicit_links: list[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    return _link_entities_for_chunk_impl(
        vault=vault,
        chunk=chunk,
        entity_catalog=entity_catalog,
        explicit_links=explicit_links,
        create_entities=True,
    )


def _link_entities_for_chunk_impl(
    *,
    vault: Path,
    chunk: Chunk,
    entity_catalog: list[dict[str, Any]],
    explicit_links: list[str],
    create_entities: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    linked: set[str] = {str(item).strip() for item in explicit_links if str(item).strip()}
    created: list[dict[str, Any]] = []
    text = chunk.body
    for entity in entity_catalog:
        if _entity_mentions_text(entity, text):
            entity_id = str(entity.get("id") or "").strip()
            if entity_id:
                linked.add(entity_id)

    candidates = _extract_entity_candidates(text)
    for candidate in candidates:
        if _find_entity_by_label(candidate, entity_catalog) is not None:
            continue
        if not _looks_like_new_reference_entity(candidate):
            continue
        if not create_entities:
            created.append({"name": candidate, "kind": "organization", "summary": f"{candidate} referenced in {chunk.title}."})
            continue
        kind = assign_kind(candidate, vault, model_kind="organization", summary=chunk.title, source_text=text)
        if kind == "thing":
            kind = "organization"
        created_entity = _ensure_entity_record(
            vault,
            candidate,
            kind=kind,
            summary=f"{candidate} referenced in {chunk.title}.",
        )
        entity_catalog.append(created_entity)
        created.append(created_entity)
        linked.add(str(created_entity["id"]))

    return sorted(linked), created


def _entity_mentions_text(entity: dict[str, Any], text: str) -> bool:
    haystack = _normalize_label(text)
    names = [_normalize_label(entity.get("name"))]
    names.extend(_normalize_label(alias) for alias in entity.get("aliases", []))
    for name in names:
        if name and re.search(rf"\b{re.escape(name)}\b", haystack):
            return True
    return False


def _extract_entity_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", text):
        candidate = match.group(1).strip()
        if len(candidate.split()) > 4:
            continue
        if candidate.lower() in {"section", "chapter", "page", "appendix"}:
            continue
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _looks_like_new_reference_entity(name: str) -> bool:
    lowered = name.lower()
    org_markers = {
        "center", "council", "board", "agency", "department", "office",
        "program", "service", "services", "authority", "administration",
        "association", "institute", "foundation", "committee", "provider",
        "network", "system", "clinic", "school", "district", "university",
        "college", "division", "team", "unit",
    }
    if any(marker in lowered.split() for marker in org_markers):
        return True
    if any(marker in lowered for marker in ("center", "program", "authority", "services")):
        return True
    return False


def _ensure_entity_record(vault: Path, name: str, *, kind: str, summary: str) -> dict[str, Any]:
    try:
        record = new_entity(
            vault,
            name,
            subtype=kind,
            summary=summary,
            confidence="high",
            confidence_basis="Reference ingestion entity linking",
            review_after=_one_year_from_today(),
        )
    except FileExistsError:
        existing = _find_entity_by_label(name, _load_entity_catalog(vault))
        if existing is not None:
            return existing
        raise
    doc = load_markdown(record.path)
    return {
        "id": str(doc.frontmatter.get("id") or ""),
        "name": str(doc.frontmatter.get("canonical_name") or doc.frontmatter.get("name") or name),
        "aliases": [str(alias).strip() for alias in doc.frontmatter.get("aliases") or [] if str(alias).strip()],
        "kind": str(doc.frontmatter.get("kind") or doc.frontmatter.get("subtype") or kind),
        "path": str(record.path),
    }


def _remove_existing_reference_chunks(vault: Path, source_document: str, *, plan_only: bool = False) -> list[str]:
    removed: list[str] = []
    for path in _existing_reference_chunk_paths(vault, source_document):
        removed.append(str(path))
        if not plan_only:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
    return removed


def _has_existing_reference_chunks(vault: Path, source_document: str) -> bool:
    return bool(_existing_reference_chunk_paths(vault, source_document))


def _existing_reference_chunk_paths(vault: Path, source_document: str) -> list[Path]:
    root = vault / "knowledge"
    if not root.exists():
        return []
    paths: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type")) != "knowledge":
            continue
        if _normalize_label(doc.frontmatter.get("source_document")) == _normalize_label(source_document):
            paths.append(path)
    return paths


def _normalize_label(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())
