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
from ..tools.common import iter_markdown_files, parse_date
from ..utils import hash_embedding


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    created DATE NOT NULL,
    updated DATE NOT NULL,
    status TEXT NOT NULL,
    significance TEXT,
    arena_primary TEXT,
    arena_secondary TEXT,
    privacy TEXT,
    compartments TEXT,
    allowed_contexts TEXT,
    blocked_contexts TEXT,
    confidence TEXT,
    confidence_basis TEXT,
    last_confirmed DATE,
    review_after DATE,
    summary TEXT,
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
    arena_context TEXT,
    classification_confidence REAL,
    files_loaded TEXT,
    files_rejected TEXT,
    rejection_reasons TEXT,
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
"""


def rebuild_index(vault: Path | None = None, db_path: Path | None = None, embeddings_file: Path | None = None) -> dict[str, int]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    embeddings_file = embeddings_file or embeddings_path()

    if db_path.exists():
        db_path.unlink()
    if embeddings_file.exists():
        embeddings_file.unlink()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
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
            fm = doc.frontmatter
            file_type = str(fm.get("type", ""))
            file_id = str(fm.get("id", ""))
            if not file_id or not file_type:
                continue
            raw = path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            word_count = len(raw.split())
            token_count = max(1, round(word_count * 1.33))
            content = f"{str(fm.get('summary', ''))}\n\n{doc.body.strip()}".strip()
            row = {
                "id": file_id,
                "type": file_type,
                "path": str(path.relative_to(vault)),
                "created": str(fm.get("created", "")),
                "updated": str(fm.get("updated", "")),
                "status": str(fm.get("status", "")),
                "significance": str(fm.get("significance", "")),
                "arena_primary": str(fm.get("arena_primary", "")),
                "arena_secondary": json.dumps(fm.get("arena_secondary", [])),
                "privacy": str(fm.get("privacy", "")),
                "compartments": json.dumps(fm.get("compartments", [])),
                "allowed_contexts": json.dumps(fm.get("allowed_contexts", [])),
                "blocked_contexts": json.dumps(fm.get("blocked_contexts", [])),
                "confidence": str(fm.get("confidence", "")),
                "confidence_basis": str(fm.get("confidence_basis", "")),
                "last_confirmed": str(fm.get("last_confirmed", "")),
                "review_after": str(fm.get("review_after", "")),
                "summary": str(fm.get("summary", "")),
                "content_hash": content_hash,
                "word_count": word_count,
                "token_count_approx": token_count,
            }
            file_rows[file_id] = row
            conn.execute(
                """
                INSERT INTO files (
                    id, type, path, created, updated, status, significance, arena_primary,
                    arena_secondary, privacy, compartments, allowed_contexts, blocked_contexts,
                    confidence, confidence_basis, last_confirmed, review_after, summary,
                    content_hash, word_count, token_count_approx
                ) VALUES (
                    :id, :type, :path, :created, :updated, :status, :significance, :arena_primary,
                    :arena_secondary, :privacy, :compartments, :allowed_contexts, :blocked_contexts,
                    :confidence, :confidence_basis, :last_confirmed, :review_after, :summary,
                    :content_hash, :word_count, :token_count_approx
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

            links = fm.get("links", []) or []
            if isinstance(links, list):
                for link in links:
                    if isinstance(link, str):
                        conn.execute(
                            "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                            (file_id, link, None),
                        )
                        counts["links"] += 1

        conn.commit()
        embeddings_file.write_text("\n".join(embeddings_lines) + ("\n" if embeddings_lines else ""), encoding="utf-8")
        return counts
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
