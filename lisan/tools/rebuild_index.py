from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, load_markdown
from ..paths import embeddings_path, repo_root, sqlite_path, vault_root
from .domain_fields import normalize_domain_fields
from .epistemic import (
    normalize_claim_frontmatter,
    normalize_evidence_frontmatter,
    normalize_skeptical_review_frontmatter,
    listify,
)
from .ingest import ensure_ingestion_manifest_table
from .ingest_batches import ensure_ingestion_batches_table
from .jobs import ensure_jobs_table
from ..tools.common import iter_markdown_files, parse_date
from ..utils import hash_embedding


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    created DATE NOT NULL,
    created_at DATE,
    updated DATE NOT NULL,
    status TEXT NOT NULL,
    significance TEXT,
    domain_primary TEXT,
    domain_secondary TEXT,
    arena TEXT,
    privacy TEXT,
    compartments TEXT,
    allowed_contexts TEXT,
    blocked_contexts TEXT,
    confidence TEXT,
    confidence_score REAL,
    confidence_basis TEXT,
    last_confirmed DATE,
    review_after DATE,
    summary TEXT,
    source_type TEXT,
    source_uri TEXT,
    artifact_ref TEXT,
    artifact_hash TEXT,
    timestamp_of_artifact TEXT,
    batch_id TEXT,
    source_path TEXT,
    file_name TEXT,
    file_ext TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    modified_at TEXT,
    imported_at TEXT,
    ingestion_status TEXT,
    extracted_text_ref TEXT,
    linked_evidence TEXT,
    parse_errors TEXT,
    actors TEXT,
    sensitivity TEXT,
    reliability TEXT,
    claim_class TEXT,
    owner TEXT,
    pattern_type TEXT,
    hypothesis TEXT,
    supporting_records TEXT,
    counterexamples TEXT,
    alternative_explanations TEXT,
    supporting_evidence TEXT,
    contradicting_evidence TEXT,
    linked_patterns TEXT,
    first_seen TEXT,
    last_reviewed TEXT,
    review_notes TEXT,
    predictions TEXT,
    evidence_needed TEXT,
    observed_facts TEXT,
    verbatim_excerpt TEXT,
    linked_claims TEXT,
    linked_episodes TEXT,
    reviewed_record_id TEXT,
    reviewed_record_type TEXT,
    approved BOOLEAN,
    risk TEXT,
    recommended_action TEXT,
    issues TEXT,
    priority_questions TEXT,
    alternative_hypotheses TEXT,
    claim_updates TEXT,
    confidence_adjustments TEXT,
    reasoning_errors TEXT,
    corrects TEXT,
    field_corrected TEXT,
    original_value TEXT,
    corrected_value TEXT,
    basis TEXT,
    approved_by TEXT,
    content_hash TEXT,
    word_count INTEGER,
    token_count_approx INTEGER
);

CREATE TABLE IF NOT EXISTS links (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relationship_type TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    sensitivity TEXT,
    source_basis TEXT,
    evidence_id TEXT,
    status TEXT NOT NULL,
    created DATE NOT NULL,
    last_reviewed DATE,
    review_after DATE
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    context TEXT,
    UNIQUE(alias, context)
);

CREATE TABLE IF NOT EXISTS entity_epochs (
    entity_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    started DATE NOT NULL,
    ended DATE,
    archived_path TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    conversation_id TEXT,
    user_query TEXT,
    domain_context TEXT,
    classification_confidence REAL,
    files_loaded TEXT,
    direct_files_loaded TEXT,
    graph_files_loaded TEXT,
    files_rejected TEXT,
    rejection_reasons TEXT,
    graph_blocked_count INTEGER,
    graph_blocked_reasons TEXT,
    token_count INTEGER,
    privacy_level TEXT,
    cross_compartment BOOLEAN,
    model_used TEXT
);

CREATE TABLE IF NOT EXISTS llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT,
    output_hash TEXT,
    schema_version TEXT,
    cost_usd REAL,
    latency_ms INTEGER,
    success BOOLEAN
);

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

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
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
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    worker_id TEXT,
    batch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
    ON jobs(status, priority, scheduled_for, created_at);

CREATE INDEX IF NOT EXISTS idx_jobs_type_status
    ON jobs(job_type, status, finished_at);

CREATE INDEX IF NOT EXISTS idx_jobs_coalesce
    ON jobs(job_type, coalesce_key, status);
"""


def rebuild_index(vault: Path | None = None, db_path: Path | None = None, embeddings_file: Path | None = None) -> dict[str, int]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    embeddings_file = embeddings_file or embeddings_path()
    jobs_backup = _backup_jobs(db_path) if db_path.exists() else []
    manifest_backup = _backup_ingestion_manifest(db_path) if db_path.exists() else []
    batch_backup = _backup_ingestion_batches(db_path) if db_path.exists() else []

    if db_path.exists():
        db_path.unlink()
    if embeddings_file.exists():
        embeddings_file.unlink()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        ensure_jobs_table(conn)
        ensure_ingestion_manifest_table(conn)
        ensure_ingestion_batches_table(conn)
        _restore_jobs(conn, jobs_backup)
        _restore_ingestion_manifest(conn, manifest_backup)
        _restore_ingestion_batches(conn, batch_backup)
        _maybe_create_fts(conn)
        counts = {"files": 0, "links": 0, "claims": 0, "aliases": 0, "epochs": 0}
        embeddings_lines: list[str] = []
        file_rows: dict[str, dict[str, Any]] = {}

        for path in iter_markdown_files(vault):
            if path.parts[-2] in {"manifests", "transcripts", "drafts"}:
                continue
            try:
                doc = load_markdown(path)
            except FrontmatterError:
                continue
            fm = normalize_domain_fields(doc.frontmatter)
            file_type = str(fm.get("type", ""))
            if file_type == "evidence":
                fm = normalize_evidence_frontmatter(fm)
            elif file_type == "claim":
                fm = normalize_claim_frontmatter(fm)
            elif file_type == "skeptical_review":
                fm = normalize_skeptical_review_frontmatter(fm)
            file_id = str(fm.get("id", ""))
            if not file_id or not file_type:
                continue
            raw = path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            word_count = len(raw.split())
            token_count = max(1, round(word_count * 1.33))
            content = f"{str(fm.get('summary', ''))}\n\n{doc.body.strip()}".strip()
            links = fm.get("links", []) or []
            if not isinstance(links, list):
                links = listify(links)
            confidence_score = None
            if file_type == "claim":
                try:
                    confidence_score = float(fm.get("confidence"))
                except (TypeError, ValueError):
                    confidence_score = None
            row = {
                "id": file_id,
                "type": file_type,
                "path": str(path.relative_to(vault)),
                "created": str(fm.get("created", "")),
                "created_at": str(fm.get("created_at", fm.get("created", ""))),
                "updated": str(fm.get("updated", "")),
                "status": str(fm.get("status", "")),
                "significance": str(fm.get("significance", "")),
                "domain_primary": str(fm.get("domain_primary") or fm.get("arena_primary") or ""),
                "domain_secondary": json.dumps(fm.get("domain_secondary") or fm.get("arena_secondary") or []),
                "arena": str(fm.get("arena") or fm.get("domain_primary") or fm.get("arena_primary") or ""),
                "privacy": str(fm.get("privacy", "")),
                "compartments": json.dumps(fm.get("compartments") or []),
                "allowed_contexts": json.dumps(fm.get("allowed_contexts") or []),
                "blocked_contexts": json.dumps(fm.get("blocked_contexts") or []),
                "confidence": str(fm.get("confidence", "")),
                "confidence_score": confidence_score,
                "confidence_basis": str(fm.get("confidence_basis", "")),
                "last_confirmed": str(fm.get("last_confirmed", "")),
                "review_after": str(fm.get("review_after", "")),
                "summary": str(fm.get("summary", "")),
                "source_type": str(fm.get("source_type") or ""),
                "source_uri": str(fm.get("source_uri", "")) if fm.get("source_uri") is not None else None,
                "artifact_ref": str(fm.get("artifact_ref", "")) if fm.get("artifact_ref") is not None else None,
                "artifact_hash": str(fm.get("artifact_hash", "")) if fm.get("artifact_hash") is not None else None,
                "timestamp_of_artifact": str(fm.get("timestamp_of_artifact", "")) if fm.get("timestamp_of_artifact") is not None else None,
                "batch_id": str(fm.get("batch_id", "")) if fm.get("batch_id") is not None else None,
                "source_path": str(fm.get("source_path", "")) if fm.get("source_path") is not None else None,
                "file_name": str(fm.get("file_name", "")) if fm.get("file_name") is not None else None,
                "file_ext": str(fm.get("file_ext", "")) if fm.get("file_ext") is not None else None,
                "mime_type": str(fm.get("mime_type", "")) if fm.get("mime_type") is not None else None,
                "size_bytes": int(fm.get("size_bytes")) if fm.get("size_bytes") is not None else None,
                "modified_at": str(fm.get("modified_at", "")) if fm.get("modified_at") is not None else None,
                "imported_at": str(fm.get("imported_at", "")) if fm.get("imported_at") is not None else None,
                "ingestion_status": str(fm.get("ingestion_status", "")) if fm.get("ingestion_status") is not None else None,
                "extracted_text_ref": str(fm.get("extracted_text_ref", "")) if fm.get("extracted_text_ref") is not None else None,
                "linked_evidence": json.dumps(listify(fm.get("linked_evidence"))),
                "parse_errors": json.dumps(listify(fm.get("parse_errors"))),
                "actors": json.dumps(listify(fm.get("actors"))),
                "sensitivity": str(fm.get("sensitivity") or ""),
                "reliability": str(fm.get("reliability") or ""),
                "claim_class": str(fm.get("claim_class") or ""),
                "owner": str(fm.get("owner") or ""),
                "pattern_type": str(fm.get("pattern_type") or ""),
                "hypothesis": str(fm.get("hypothesis") or ""),
                "supporting_records": json.dumps(listify(fm.get("supporting_records"))),
                "counterexamples": json.dumps(listify(fm.get("counterexamples"))),
                "alternative_explanations": json.dumps(listify(fm.get("alternative_explanations"))),
                "supporting_evidence": json.dumps(listify(fm.get("supporting_evidence"))),
                "contradicting_evidence": json.dumps(listify(fm.get("contradicting_evidence"))),
                "linked_patterns": json.dumps(listify(fm.get("linked_patterns"))),
                "first_seen": str(fm.get("first_seen", "")) if fm.get("first_seen") is not None else None,
                "last_reviewed": str(fm.get("last_reviewed", "")) if fm.get("last_reviewed") is not None else None,
                "review_notes": str(fm.get("review_notes", "")),
                "predictions": json.dumps(listify(fm.get("predictions"))),
                "evidence_needed": json.dumps(listify(fm.get("evidence_needed"))),
                "observed_facts": json.dumps(listify(fm.get("observed_facts"))),
                "verbatim_excerpt": str(fm.get("verbatim_excerpt", "")),
                "linked_claims": json.dumps(listify(fm.get("linked_claims"))),
                "linked_episodes": json.dumps(listify(fm.get("linked_episodes"))),
                "reviewed_record_id": str(fm.get("reviewed_record_id") or ""),
                "reviewed_record_type": str(fm.get("reviewed_record_type") or ""),
                "approved": int(bool(fm.get("approved"))) if fm.get("approved") is not None else None,
                "risk": str(fm.get("risk") or ""),
                "recommended_action": str(fm.get("recommended_action") or ""),
                "issues": json.dumps(fm.get("issues") or []),
                "priority_questions": json.dumps(listify(fm.get("priority_questions"))),
                "alternative_hypotheses": json.dumps(listify(fm.get("alternative_hypotheses"))),
                "evidence_needed": json.dumps(listify(fm.get("evidence_needed"))),
                "claim_updates": json.dumps(fm.get("claim_updates") or []),
                "confidence_adjustments": json.dumps(fm.get("confidence_adjustments") or []),
                "reasoning_errors": json.dumps(listify(fm.get("reasoning_errors"))),
                "corrects": str(fm.get("corrects") or ""),
                "field_corrected": str(fm.get("field_corrected") or ""),
                "original_value": str(fm.get("original_value") or ""),
                "corrected_value": str(fm.get("corrected_value") or ""),
                "basis": str(fm.get("basis") or ""),
                "approved_by": str(fm.get("approved_by") or ""),
                "content_hash": content_hash,
                "word_count": word_count,
                "token_count_approx": token_count,
            }
            file_rows[file_id] = row
            conn.execute(
                """
                INSERT INTO files (
                    id, type, path, created, created_at, updated, status, significance, domain_primary,
                    domain_secondary, arena, privacy, compartments, allowed_contexts, blocked_contexts,
                    confidence, confidence_score, confidence_basis, last_confirmed, review_after, summary,
                    source_type, source_uri, artifact_ref, artifact_hash, timestamp_of_artifact,
                    batch_id, source_path, file_name, file_ext, mime_type, size_bytes, modified_at, imported_at,
                    ingestion_status, extracted_text_ref, linked_evidence, parse_errors,
                    actors, sensitivity, reliability, claim_class, owner, pattern_type, hypothesis,
                    supporting_records, counterexamples, alternative_explanations, supporting_evidence,
                    contradicting_evidence, linked_patterns, first_seen, last_reviewed, review_notes,
                    predictions, evidence_needed, observed_facts, verbatim_excerpt, linked_claims, linked_episodes, reviewed_record_id,
                    reviewed_record_type, approved, risk, recommended_action, issues, priority_questions,
                    alternative_hypotheses, claim_updates, confidence_adjustments,
                    reasoning_errors, corrects, field_corrected, original_value, corrected_value, basis,
                    approved_by, content_hash, word_count, token_count_approx
                ) VALUES (
                    :id, :type, :path, :created, :created_at, :updated, :status, :significance, :domain_primary,
                    :domain_secondary, :arena, :privacy, :compartments, :allowed_contexts, :blocked_contexts,
                    :confidence, :confidence_score, :confidence_basis, :last_confirmed, :review_after, :summary,
                    :source_type, :source_uri, :artifact_ref, :artifact_hash, :timestamp_of_artifact,
                    :batch_id, :source_path, :file_name, :file_ext, :mime_type, :size_bytes, :modified_at, :imported_at,
                    :ingestion_status, :extracted_text_ref, :linked_evidence, :parse_errors,
                    :actors, :sensitivity, :reliability, :claim_class, :owner, :pattern_type, :hypothesis,
                    :supporting_records, :counterexamples, :alternative_explanations, :supporting_evidence,
                    :contradicting_evidence, :linked_patterns, :first_seen, :last_reviewed, :review_notes,
                    :predictions, :evidence_needed, :observed_facts, :verbatim_excerpt, :linked_claims, :linked_episodes, :reviewed_record_id,
                    :reviewed_record_type, :approved, :risk, :recommended_action, :issues, :priority_questions,
                    :alternative_hypotheses, :claim_updates, :confidence_adjustments,
                    :reasoning_errors, :corrects, :field_corrected, :original_value, :corrected_value, :basis,
                    :approved_by, :content_hash, :word_count, :token_count_approx
                )
                """,
                row,
            )
            counts["files"] += 1
            embeddings_lines.append(json.dumps({"id": file_id, "embedding": hash_embedding(content)}))
            try:
                conn.execute(
                    "INSERT INTO files_fts (id, summary, content) VALUES (?, ?, ?)",
                    (file_id, str(fm.get("summary", "")), content),
                )
            except sqlite3.Error:
                pass

            if file_type == "entity":
                # Index canonical name first so the heuristic gate can find entities by name
                canonical = str(fm.get("canonical_name") or fm.get("id") or "").strip()
                if canonical:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
                            (file_id, canonical, None),
                        )
                        counts["aliases"] += 1
                    except sqlite3.Error:
                        pass
                for alias in fm.get("aliases", []) or []:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
                            (file_id, str(alias), None),
                        )
                        counts["aliases"] += 1
                    except sqlite3.Error:
                        pass
                previous_epochs = fm.get("previous_epochs", []) or []
                if isinstance(previous_epochs, list):
                    for previous in previous_epochs:
                        if not isinstance(previous, dict):
                            continue
                        conn.execute(
                            "INSERT INTO entity_epochs (entity_id, epoch, started, ended, archived_path, summary) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                file_id,
                                int(previous.get("epoch", 0)),
                                str(previous.get("period", "").split(" to ")[0].replace("YYYY-MM", "") or fm.get("epoch_started", "")),
                                None,
                                str(previous.get("archived", "")),
                                str(previous.get("summary", "")),
                            ),
                        )
                        counts["epochs"] += 1
                conn.execute(
                    "INSERT INTO entity_epochs (entity_id, epoch, started, ended, archived_path, summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, int(fm.get("epoch", 0) or 0), str(fm.get("epoch_started", "")), None, None, str(fm.get("summary", ""))),
                )
                counts["epochs"] += 1

            if file_type == "episode":
                for claim in _extract_claims_from_episode(doc.body, file_id):
                    conn.execute(
                        """
                        INSERT INTO claims (
                            id, episode_id, claim_text, claim_type, confidence, sensitivity,
                            source_basis, evidence_id, status, created, last_reviewed, review_after
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        claim,
                    )
                    counts["claims"] += 1

            # Standalone claim records (written by the capture fanout) belong
            # in the claims table too — without this, the writer's claims
            # never make it to retrieval / contradiction detection / Dreamer.
            if file_type == "claim":
                episode_id = ""
                for link_target in listify(fm.get("linked_episodes")) or links:
                    text = str(link_target).strip()
                    if text:
                        episode_id = text
                        break
                claim_row = (
                    file_id,
                    episode_id,
                    str(fm.get("claim_text") or fm.get("summary") or ""),
                    str(fm.get("claim_class") or "interpretation"),
                    str(fm.get("confidence") or "0.5"),
                    str(fm.get("sensitivity") or "low"),
                    str(fm.get("confidence_basis") or fm.get("review_notes") or ""),
                    ", ".join(listify(fm.get("supporting_evidence"))),
                    str(fm.get("status") or "active"),
                    str(fm.get("created") or fm.get("first_seen") or ""),
                    str(fm.get("last_reviewed") or ""),
                    str(fm.get("review_after") or ""),
                )
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO claims (
                            id, episode_id, claim_text, claim_type, confidence, sensitivity,
                            source_basis, evidence_id, status, created, last_reviewed, review_after
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        claim_row,
                    )
                    counts["claims"] += 1
                except sqlite3.Error:
                    pass

            if isinstance(links, list):
                for link in links:
                    if isinstance(link, str):
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                            (file_id, link, None),
                        )
                        counts["links"] += 1
            if file_type == "artifact":
                for target in listify(fm.get("linked_evidence")):
                    if target:
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                            (file_id, target, "linked_evidence"),
                        )
                        counts["links"] += 1
                for target in listify(fm.get("linked_claims")):
                    if target:
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                            (file_id, target, "linked_claims"),
                        )
                        counts["links"] += 1
            if file_type in {"evidence", "claim"}:
                artifact_ref = str(fm.get("artifact_ref") or "").strip()
                if artifact_ref:
                    conn.execute(
                        "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                        (artifact_ref, file_id, "artifact_provenance"),
                    )
                    counts["links"] += 1
            for source_key, relationship in [
                ("supporting_evidence", "supports"),
                ("contradicting_evidence", "contradicts"),
                ("linked_claims", "links_claim"),
                ("linked_episodes", "links_episode"),
            ]:
                for target in listify(fm.get(source_key)):
                    if target:
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                            (file_id, target, relationship),
                            )
                        counts["links"] += 1
            for target_key, relationship in [("reviewed_record_id", "reviews"), ("corrects", "corrects")]:
                target = str(fm.get(target_key, "")).strip()
                if target:
                    conn.execute(
                        "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                        (file_id, target, relationship),
                    )
                    counts["links"] += 1

        conn.commit()
        embeddings_file.write_text("\n".join(embeddings_lines) + ("\n" if embeddings_lines else ""), encoding="utf-8")
        return counts
    finally:
        conn.close()


def _backup_jobs(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _backup_ingestion_manifest(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute("SELECT * FROM ingestion_manifest").fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _backup_ingestion_batches(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute("SELECT * FROM ingestion_batches").fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _restore_jobs(conn: sqlite3.Connection, jobs_backup: list[dict[str, Any]]) -> None:
    if not jobs_backup:
        return
    for job in jobs_backup:
        row = {
            "id": job.get("id"),
            "job_type": job.get("job_type"),
            "status": job.get("status"),
            "priority": job.get("priority", 100),
            "batch_id": job.get("batch_id"),
            "coalesce_key": job.get("coalesce_key"),
            "unique_group": job.get("unique_group"),
            "replaces_job_id": job.get("replaces_job_id"),
            "coalesced_count": job.get("coalesced_count", 0),
            "payload_json": job.get("payload_json") or "{}",
            "result_json": job.get("result_json"),
            "result_ref": job.get("result_ref"),
            "attempts": job.get("attempts", 0),
            "max_attempts": job.get("max_attempts", 3),
            "created_at": job.get("created_at"),
            "scheduled_for": job.get("scheduled_for"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "error": job.get("error"),
            "worker_id": job.get("worker_id"),
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs (
                id, job_type, status, priority, batch_id, coalesce_key, unique_group, replaces_job_id,
                coalesced_count, payload_json, result_json, result_ref, attempts, max_attempts,
                created_at, scheduled_for, started_at, finished_at, error, worker_id
            ) VALUES (
                :id, :job_type, :status, :priority, :batch_id, :coalesce_key, :unique_group, :replaces_job_id,
                :coalesced_count, :payload_json, :result_json, :result_ref, :attempts, :max_attempts,
                :created_at, :scheduled_for, :started_at, :finished_at, :error, :worker_id
            )
            """,
            row,
        )


def _restore_ingestion_manifest(conn: sqlite3.Connection, manifest_backup: list[dict[str, Any]]) -> None:
    if not manifest_backup:
        return
    for row in manifest_backup:
        normalized = dict(row)
        normalized.setdefault("batch_id", None)
        conn.execute(
            """
            INSERT OR REPLACE INTO ingestion_manifest (
                source_path, artifact_hash, last_seen, last_ingested, status, artifact_id, batch_id, error
            ) VALUES (
                :source_path, :artifact_hash, :last_seen, :last_ingested, :status, :artifact_id, :batch_id, :error
            )
            """,
            normalized,
        )


def _restore_ingestion_batches(conn: sqlite3.Connection, batch_backup: list[dict[str, Any]]) -> None:
    if not batch_backup:
        return
    for row in batch_backup:
        conn.execute(
            """
            INSERT OR REPLACE INTO ingestion_batches (
                id, source_root, mode, status, created_at, started_at, finished_at, requested_by,
                options_json, summary_json, error, notes
            ) VALUES (
                :id, :source_root, :mode, :status, :created_at, :started_at, :finished_at, :requested_by,
                :options_json, :summary_json, :error, :notes
            )
            """,
            row,
        )


def _extract_claims_from_episode(body: str, episode_id: str) -> list[tuple[Any, ...]]:
    lines = body.splitlines()
    claims: list[tuple[Any, ...]] = []
    in_claims = False
    table_lines: list[str] = []
    for line in lines:
        if line.strip() == "## Claims":
            in_claims = True
            continue
        if in_claims and line.startswith("## "):
            break
        if in_claims:
            if "|" in line:
                table_lines.append(line)
    rows = [line for line in table_lines if line.strip().startswith("|")]
    if len(rows) < 3:
        return []
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        data = dict(zip(headers, cells))
        claim_id = data.get("ID") or data.get("Id") or data.get("id")
        if not claim_id:
            continue
        claim_text = str(data.get("Claim") or data.get("claim") or "")
        sensitivity = _detect_claim_sensitivity(claim_text)
        claims.append(
            (
                claim_id,
                episode_id,
                claim_text,
                data.get("Type") or data.get("type") or "reported",
                data.get("Confidence") or data.get("confidence") or "low",
                sensitivity,
                data.get("Source") or data.get("source") or None,
                data.get("Evidence") or data.get("evidence") or None,
                data.get("Status") or data.get("status") or "unresolved",
                data.get("Created") or data.get("created") or "",
                data.get("Last reviewed") or data.get("last_reviewed") or None,
                data.get("Review after") or data.get("review_after") or None,
            )
        )
    return claims


_PROFESSIONAL_REVIEW_TERMS = frozenset({
    "criminal", "custody", "elder abuse", "abuse", "medical", "diagnosis",
    "symptoms", "tax", "insurance fraud", "fraud", "legal obligation",
    "custody implication", "medication", "prescription",
})


def _detect_claim_sensitivity(claim_text: str) -> str | None:
    lowered = claim_text.lower()
    if any(term in lowered for term in _PROFESSIONAL_REVIEW_TERMS):
        return "requires_professional_review"
    return None


def _maybe_create_fts(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(id, summary, content, tokenize='porter')")
    except sqlite3.OperationalError:
        conn.execute("CREATE TABLE IF NOT EXISTS files_fts (id TEXT, summary TEXT, content TEXT)")


def main(argv: list[str] | None = None) -> int:
    counts = rebuild_index()
    print(f"Index rebuilt: {counts}")
    return 0
