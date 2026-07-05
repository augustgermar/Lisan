from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import FrontmatterError, load_markdown
from ..paths import embeddings_path, repo_root, sqlite_path, vault_root
from ..providers.embeddings import EmbeddingProvider
from .domain_fields import normalize_domain_fields
from ..utils import listify
from .epistemic import (
    normalize_claim_frontmatter,
    normalize_evidence_frontmatter,
    normalize_skeptical_review_frontmatter,
)
from .ingest import ensure_ingestion_manifest_table
from .ingest_batches import ensure_ingestion_batches_table
from .jobs import ensure_jobs_table
from .common import iter_markdown_files, parse_date
from .vector_store import clear_index_cache, load_index, write_embeddings


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
    disclosure TEXT,
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
    token_count_approx INTEGER,
    embedding_status TEXT
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
    model_used TEXT,
    retrieval_mode TEXT,
    fusion_enabled BOOLEAN,
    sql_candidate_count INTEGER,
    fts_candidate_count INTEGER,
    vector_candidate_count INTEGER,
    fused_candidate_count INTEGER,
    overlap_count INTEGER,
    rrf_k INTEGER,
    per_layer_limit INTEGER,
    fused_limit INTEGER,
    fts_mode TEXT,
    embedding_mode TEXT
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

    if embeddings_file.exists():
        embeddings_file.unlink()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_index_schema(conn)
        for table in ("files", "links", "claims", "entity_aliases", "entity_epochs"):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM files_fts")
        except sqlite3.Error:
            pass
        conn.commit()
        for path in iter_markdown_files(vault):
            index_single_record(path, vault, conn)
        conn.commit()
        embed_targets = _embed_targets_from_index(vault, conn)
        _embed_and_write(conn, embed_targets, embeddings_file)
        conn.commit()
        counts = _index_counts(conn)
        clear_index_cache()
        return counts
    finally:
        conn.close()


def open_index_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite index connection with the files schema ready for writes."""
    conn = sqlite3.connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    return conn


def ensure_index_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _ensure_files_columns(conn)
    ensure_jobs_table(conn)
    ensure_ingestion_manifest_table(conn)
    ensure_ingestion_batches_table(conn)
    _maybe_create_fts(conn)


def index_single_record(path: Path, vault: Path, conn: sqlite3.Connection) -> bool:
    """Index one markdown record into files + files_fts (+ side tables).

    Returns True if indexed, False if skipped. This mirrors full rebuild's
    per-file logic and leaves embedding_status='pending' for the async embed
    sweep.
    """
    try:
        rel = path.relative_to(vault)
    except ValueError:
        rel = path
    parent = rel.parts[-2] if len(rel.parts) >= 2 else ""
    if parent in {"manifests", "transcripts"}:
        return False
    if parent == "drafts" and "needs_revision" not in path.name:
        return False
    try:
        doc = load_markdown(path)
    except FrontmatterError:
        return False

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
        return False

    raw = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    word_count = len(raw.split())
    token_count = max(1, round(word_count * 1.33))
    content = f"{str(fm.get('summary', ''))}\n\n{doc.body.strip()}".strip()
    # An entity's durable source_log is part of its searchable content: a fact
    # compaction left out of the narrative prose is still findable via the log.
    if fm.get("source_log"):
        from .entity_story import entity_search_text

        content = entity_search_text(fm, doc.body)
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
        "path": str(rel),
        "created": str(fm.get("created", "")),
        "created_at": str(fm.get("created_at", fm.get("created", ""))),
        "updated": str(fm.get("updated", "")),
        "status": str(fm.get("status", "")),
        "significance": str(fm.get("significance", "")),
        "domain_primary": str(fm.get("domain_primary") or fm.get("arena_primary") or ""),
        "domain_secondary": json.dumps(fm.get("domain_secondary") or fm.get("arena_secondary") or []),
        "arena": str(fm.get("arena") or fm.get("domain_primary") or fm.get("arena_primary") or ""),
        "privacy": str(fm.get("privacy", "")),
        "disclosure": str(fm.get("disclosure", "private")),
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
        "embedding_status": "pending",
    }

    conn.execute("DELETE FROM links WHERE source_id = ?", (file_id,))
    conn.execute("DELETE FROM links WHERE target_id = ? AND relationship_type = ?", (file_id, "artifact_provenance"))
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (file_id,))
    conn.execute("DELETE FROM entity_epochs WHERE entity_id = ?", (file_id,))
    conn.execute("DELETE FROM claims WHERE id = ?", (file_id,))
    try:
        conn.execute("DELETE FROM files_fts WHERE id = ?", (file_id,))
    except sqlite3.Error:
        pass
    conn.execute(
        """
        INSERT OR REPLACE INTO files (
            id, type, path, created, created_at, updated, status, significance, domain_primary,
            domain_secondary, arena, privacy, disclosure, compartments, allowed_contexts, blocked_contexts,
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
            approved_by, content_hash, word_count, token_count_approx, embedding_status
        ) VALUES (
            :id, :type, :path, :created, :created_at, :updated, :status, :significance, :domain_primary,
            :domain_secondary, :arena, :privacy, :disclosure, :compartments, :allowed_contexts, :blocked_contexts,
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
            :approved_by, :content_hash, :word_count, :token_count_approx, :embedding_status
        )
        """,
        row,
    )
    try:
        conn.execute(
            "INSERT INTO files_fts (id, summary, content) VALUES (?, ?, ?)",
            (file_id, str(fm.get("summary", "")), content),
        )
    except sqlite3.Error:
        pass

    if file_type == "entity":
        canonical = str(fm.get("canonical_name") or fm.get("id") or "").strip()
        if canonical:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
                (file_id, canonical, None),
            )
        nickname = str(fm.get("nickname") or "").strip()
        if nickname:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
                (file_id, nickname, "nickname"),
            )
        for alias in fm.get("aliases", []) or []:
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (entity_id, alias, context) VALUES (?, ?, ?)",
                (file_id, str(alias), None),
            )
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
        conn.execute(
            "INSERT INTO entity_epochs (entity_id, epoch, started, ended, archived_path, summary) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, int(fm.get("epoch", 0) or 0), str(fm.get("epoch_started", "")), None, None, str(fm.get("summary", ""))),
        )

    if file_type == "episode":
        for claim in _extract_claims_from_episode(doc.body, file_id):
            conn.execute(
                """
                INSERT OR REPLACE INTO claims (
                    id, episode_id, claim_text, claim_type, confidence, sensitivity,
                    source_basis, evidence_id, status, created, last_reviewed, review_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                claim,
            )

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
        conn.execute(
            """
            INSERT OR REPLACE INTO claims (
                id, episode_id, claim_text, claim_type, confidence, sensitivity,
                source_basis, evidence_id, status, created, last_reviewed, review_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            claim_row,
        )

    for link in links:
        if isinstance(link, str):
            conn.execute(
                "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                (file_id, link, "related"),
            )
    if file_type == "artifact":
        for target in listify(fm.get("linked_evidence")):
            if target:
                conn.execute(
                    "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                    (file_id, target, "linked_evidence"),
                )
        for target in listify(fm.get("linked_claims")):
            if target:
                conn.execute(
                    "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                    (file_id, target, "linked_claims"),
                )
    if file_type in {"evidence", "claim"}:
        artifact_ref = str(fm.get("artifact_ref") or "").strip()
        if artifact_ref:
            conn.execute(
                "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                (artifact_ref, file_id, "artifact_provenance"),
            )
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
    for target_key, relationship in [("reviewed_record_id", "reviews"), ("corrects", "corrects")]:
        target = str(fm.get(target_key, "")).strip()
        if target:
            conn.execute(
                "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                (file_id, target, relationship),
            )
    return True


def _embed_targets_from_index(vault: Path, conn: sqlite3.Connection) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    rows = conn.execute("SELECT id, path, summary FROM files ORDER BY path").fetchall()
    for row in rows:
        try:
            doc = load_markdown(vault / str(row["path"]))
        except (FrontmatterError, OSError):
            continue
        content = f"{str(row['summary'] or '')}\n\n{doc.body.strip()}".strip()
        targets.append((str(row["id"]), content))
    return targets


def _index_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "files": int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
        "links": int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]),
        "claims": int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]),
        "aliases": int(conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]),
        "epochs": int(conn.execute("SELECT COUNT(*) FROM entity_epochs").fetchone()[0]),
    }


def _ensure_files_columns(conn: sqlite3.Connection) -> None:
    """Backfill columns added after a database was first created."""
    try:
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    except sqlite3.Error:
        return
    if "disclosure" not in existing:
        try:
            conn.execute("ALTER TABLE files ADD COLUMN disclosure TEXT")
        except sqlite3.Error:
            pass
    if "embedding_status" not in existing:
        try:
            conn.execute("ALTER TABLE files ADD COLUMN embedding_status TEXT")
        except sqlite3.Error:
            pass


def _embed_and_write(
    conn: sqlite3.Connection,
    embed_targets: list[tuple[str, str]],
    embeddings_file: Path,
) -> None:
    """Embed all record contents in batched passes and write embeddings.bin with
    a model+dimension header. Records whose vector could not be produced (the
    embedder was unreachable under unreachable_policy:skip) are written with no
    vector and left as embedding_status='pending' for a later sweep."""
    provider = EmbeddingProvider(load_config())
    batch_size = max(1, int(provider.settings.get("batch_size", 64) or 64))

    records: list[tuple[str, list[float] | None]] = []
    index_model = "none"
    index_dimension = 0
    saw_semantic = False

    for start in range(0, len(embed_targets), batch_size):
        chunk = embed_targets[start : start + batch_size]
        outcome = provider.embed_records([content for _, content in chunk])
        for (file_id, _content), vector in zip(chunk, outcome.vectors):
            records.append((file_id, vector))
            if vector is None:
                status = "pending"
            elif outcome.mode_used == "hash":
                status = "hash"
            else:
                status = "embedded"
            conn.execute(
                "UPDATE files SET embedding_status = ? WHERE id = ?",
                (status, file_id),
            )
        if outcome.mode_used == "semantic":
            saw_semantic = True
            index_model = outcome.model
            index_dimension = outcome.dimension
        elif outcome.mode_used == "hash" and not saw_semantic:
            index_model = outcome.model
            index_dimension = outcome.dimension

    write_embeddings(embeddings_file, records, model=index_model, dimension=index_dimension)


def embed_pending_records(
    vault: Path | None = None,
    db_path: Path | None = None,
    embeddings_file: Path | None = None,
) -> dict[str, Any]:
    """Drain records left at embedding_status='pending' (e.g. captured while the
    embedder was down) by embedding them and merging the vectors into the
    existing index — without requiring a full rebuild. Reuses the same
    EmbeddingProvider transport as the full rebuild.

    Exposed as the ``index.embed_pending`` job type so the existing job queue /
    sync sweep can recover semantic coverage incrementally."""
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    embeddings_file = embeddings_file or embeddings_path()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_files_columns(conn)
        pending = conn.execute(
            "SELECT id, path FROM files WHERE COALESCE(embedding_status, 'pending') = 'pending'"
        ).fetchall()
        if not pending:
            return {"pending": 0, "embedded": 0, "still_pending": 0, "mode_used": "none"}

        targets: list[tuple[str, str]] = []
        for row in pending:
            doc_path = vault / str(row["path"])
            try:
                doc = load_markdown(doc_path)
            except (FrontmatterError, OSError):
                continue
            content = f"{str(doc.frontmatter.get('summary', ''))}\n\n{doc.body.strip()}".strip()
            targets.append((str(row["id"]), content))

        provider = EmbeddingProvider(load_config())
        existing = load_index(embeddings_file)
        merged: dict[str, list[float]] = dict(existing.vectors)
        index_model = existing.model
        index_dimension = existing.dimension

        embedded = 0
        mode_used = "skip"
        batch_size = max(1, int(provider.settings.get("batch_size", 64) or 64))
        for start in range(0, len(targets), batch_size):
            chunk = targets[start : start + batch_size]
            outcome = provider.embed_records([content for _, content in chunk])
            mode_used = outcome.mode_used
            if outcome.mode_used == "skip":
                break  # still unreachable; leave the rest pending
            if outcome.mode_used == "semantic":
                if index_dimension and existing.vectors and outcome.dimension != index_dimension:
                    raise ValueError(
                        f"pending re-embed dimension {outcome.dimension} != index dimension "
                        f"{index_dimension}; the embedding model changed — run `lisan rebuild-index`"
                    )
                index_model = outcome.model
                index_dimension = outcome.dimension
            for (file_id, _content), vector in zip(chunk, outcome.vectors):
                if vector is None:
                    continue
                merged[file_id] = vector
                status = "hash" if outcome.mode_used == "hash" else "embedded"
                conn.execute("UPDATE files SET embedding_status = ? WHERE id = ?", (status, file_id))
                embedded += 1

        conn.commit()
        write_embeddings(
            embeddings_file,
            list(merged.items()),
            model=index_model,
            dimension=index_dimension,
        )
        clear_index_cache()
        still_pending = conn.execute(
            "SELECT COUNT(*) FROM files WHERE COALESCE(embedding_status, 'pending') = 'pending'"
        ).fetchone()[0]
        return {
            "pending": len(targets),
            "embedded": embedded,
            "still_pending": int(still_pending),
            "mode_used": mode_used,
        }
    finally:
        conn.close()



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
