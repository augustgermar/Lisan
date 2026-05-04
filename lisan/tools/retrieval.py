from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import embeddings_path, sqlite_path, vault_root
from ..tools.common import iter_markdown_files
from ..utils import approx_word_count


ARENA_KEYWORDS: dict[str, set[str]] = {
    "physical": {"body", "sleep", "health", "fitness", "exercise", "pain", "energy"},
    "environmental": {"home", "room", "space", "environment", "apartment", "house"},
    "financial": {"money", "budget", "cash", "expense", "income", "tax", "finance"},
    "relational": {"friend", "partner", "family", "relationship", "dating", "marriage"},
    "work": {"work", "job", "project", "team", "meeting", "deadline", "client"},
    "status": {"status", "reputation", "credibility", "resume"},
    "appearance": {"appearance", "look", "style", "dress", "presentation"},
    "competence": {"competence", "skill", "capable", "ability", "performance"},
    "social_presence": {"social", "presence", "network", "community", "visibility"},
    "desirability": {"desirable", "attractive", "desired", "romantic"},
}

SENSITIVE_COMPARTMENTS = {
    "legal": {"legal", "lawsuit", "contract", "attorney", "court"},
    "health": {"health", "medical", "doctor", "diagnosis", "therapy"},
    "children": {"child", "children", "kid", "custody", "school"},
}


@dataclass(slots=True)
class RetrievalItem:
    id: str
    type: str
    path: str
    summary: str
    score: float
    reason: str


@dataclass(slots=True)
class RetrievalResult:
    arena: str
    confidence: float
    loaded: list[RetrievalItem]
    rejected: list[RetrievalItem]
    prompt: str


def assemble_context(query: str, arena: str | None = None, vault: Path | None = None, db_path: Path | None = None, conversation_id: str | None = None) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    result = retrieve_context(query=query, arena=arena, vault=vault, db_path=db_path, conversation_id=conversation_id)
    sections: list[str] = ["# Assembled Context", ""]
    sections.append(f"arena: {result.arena}")
    sections.append(f"arena_confidence: {result.confidence:.2f}")
    sections.append("")
    sections.append(f"query: {query}")
    sections.append("")

    for rel in ["primer/identity.md", "primer/operating-style.md", "primer/current-brief.md"]:
        path = vault / rel
        if path.exists():
            sections.append(f"## {rel}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    sections.append("## State")
    state_files = [item for item in result.loaded if item.type == "state"]
    if state_files:
        for item in state_files:
            sections.append(f"### {item.path}")
            sections.append((vault / item.path).read_text(encoding="utf-8").strip())
            sections.append("")
    else:
        sections.append("- None")
        sections.append("")

    sections.append("## Relevant Records")
    if result.loaded:
        for item in result.loaded:
            sections.append(f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}")
    else:
        sections.append("- None")
    sections.append("")

    if result.rejected:
        sections.append("## Rejected By Compartment")
        for item in result.rejected:
            sections.append(f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}")
        sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def retrieve_context(
    query: str,
    arena: str | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
    conversation_id: str | None = None,
) -> RetrievalResult:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    inferred_arena, confidence = _infer_arena(query, arena)
    active_contexts = _active_contexts(inferred_arena, query)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        fts_ids = _fts_candidate_ids(conn, query)
        file_rows = conn.execute(
            "SELECT id, type, path, summary, arena_primary, arena_secondary, privacy, compartments, allowed_contexts, blocked_contexts, significance, status, updated, created FROM files"
        ).fetchall()
        all_items = [_score_row(row, query, inferred_arena, vault, active_contexts, fts_ids) for row in file_rows]
        loaded = [item for item in all_items if item is not None]
        loaded.sort(key=lambda item: (item.score, item.summary), reverse=True)
        loaded = loaded[:15]
        loaded_ids = {item.id for item in loaded}
        rejected = [item for item in all_items if item is not None and item.id not in loaded_ids and item.reason.startswith("compartment")]
        _log_retrieval(
            conn,
            conversation_id=conversation_id,
            query=query,
            arena=inferred_arena,
            confidence=confidence,
            loaded=loaded,
            rejected=rejected,
        )
        return RetrievalResult(arena=inferred_arena, confidence=confidence, loaded=loaded, rejected=rejected, prompt=query)
    finally:
        conn.close()


def _infer_arena(query: str, explicit: str | None) -> tuple[str, float]:
    if explicit:
        return explicit, 1.0
    lowered = query.lower()
    best = "cross_arena"
    best_score = 0
    for arena, keywords in ARENA_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score > best_score:
            best = arena
            best_score = score
    confidence = min(1.0, 0.35 + (best_score * 0.15))
    if best_score == 0:
        return "cross_arena", 0.25
    return best, confidence


def _active_contexts(arena: str, query: str) -> set[str]:
    contexts = {arena, "all"}
    lowered = query.lower()
    for compartment, keywords in SENSITIVE_COMPARTMENTS.items():
        if any(keyword in lowered for keyword in keywords):
            contexts.add(compartment)
    if arena == "cross_arena":
        contexts.add("cross_arena")
    return contexts


def _score_row(
    row: sqlite3.Row,
    query: str,
    arena: str,
    vault: Path,
    active_contexts: set[str],
    fts_ids: set[str],
) -> RetrievalItem | None:
    allowed_contexts = _json_list(row["allowed_contexts"])
    blocked_contexts = _json_list(row["blocked_contexts"])
    compartments = _json_list(row["compartments"])
    file_contexts = set(allowed_contexts or ["all"]) | set(compartments)
    if blocked_contexts and file_contexts.intersection(blocked_contexts):
        return RetrievalItem(
            id=str(row["id"]),
            type=str(row["type"]),
            path=str(row["path"]),
            summary=str(row["summary"]),
            score=0.0,
            reason="compartment_blocked",
        )
    if allowed_contexts and "all" not in allowed_contexts and not file_contexts.intersection(active_contexts):
        return RetrievalItem(
            id=str(row["id"]),
            type=str(row["type"]),
            path=str(row["path"]),
            summary=str(row["summary"]),
            score=0.0,
            reason="compartment_blocked",
        )

    score = 0.0
    reasons: list[str] = []
    lowered = query.lower()
    summary = str(row["summary"])
    haystack = f"{summary}\n{row['id']}\n{row['path']}".lower()

    if row["arena_primary"] == arena or row["arena_primary"] == "cross_arena":
        score += 2.0
        reasons.append("arena_match")
    if row["type"] == "state":
        score += 2.0
    elif row["type"] == "decision":
        score += 1.5
    elif row["type"] == "open_loop":
        score += 1.5
    elif row["type"] == "episode":
        score += 1.0
    elif row["type"] == "entity":
        score += 0.8

    query_terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", lowered) if len(term) > 2]
    if query_terms and any(term in haystack for term in query_terms):
        score += 2.5
        reasons.append("text_match")

    if str(row["id"]) in fts_ids:
        score += 2.0
        reasons.append("fts")

    vector_score = _vector_score(query, row["id"], vault)
    if vector_score:
        score += vector_score * 2.0
        reasons.append("vector")

    if str(row["status"]) == "active":
        score += 0.5
    if str(row["significance"]) == "high":
        score += 0.7

    if not reasons:
        return None

    return RetrievalItem(
        id=str(row["id"]),
        type=str(row["type"]),
        path=str(row["path"]),
        summary=summary,
        score=round(score, 3),
        reason=",".join(reasons),
    )


def _vector_score(query: str, file_id: str, vault: Path) -> float:
    query_vec = _hash_embedding(query)
    emb_path = embeddings_path()
    if not emb_path.exists():
        return 0.0
    best = 0.0
    with emb_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(payload.get("id")) != file_id:
                continue
            vector = payload.get("embedding")
            if not isinstance(vector, list):
                continue
            best = max(best, _cosine(query_vec, vector))
    return best


def _cosine(a: list[float], b: list[float]) -> float:
    length = min(len(a), len(b))
    if not length:
        return 0.0
    dot = sum(a[i] * float(b[i]) for i in range(length))
    a_norm = math.sqrt(sum(v * v for v in a)) or 1.0
    b_norm = math.sqrt(sum(float(v) * float(v) for v in b[:length])) or 1.0
    return dot / (a_norm * b_norm)


def _hash_embedding(text: str, dimensions: int = 32) -> list[float]:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [0.0] * dimensions
    for index, byte in enumerate(digest):
        vector[index % dimensions] += byte / 255.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _fts_candidate_ids(conn: sqlite3.Connection, query: str) -> set[str]:
    terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query.lower()) if len(term) > 2]
    if not terms:
        return set()
    fts_query = " OR ".join(f'"{_fts_escape(term)}"' for term in terms)
    try:
        rows = conn.execute("SELECT id FROM files_fts WHERE files_fts MATCH ?", (fts_query,)).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row["id"]) for row in rows if row["id"]}


def _fts_escape(term: str) -> str:
    return term.replace('"', "").replace("'", "")


def _json_list(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
    except Exception:
        pass
    return [str(value)]


def _log_retrieval(
    conn: sqlite3.Connection,
    conversation_id: str | None,
    query: str,
    arena: str,
    confidence: float,
    loaded: list[RetrievalItem],
    rejected: list[RetrievalItem],
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO retrieval_log (
                conversation_id, user_query, arena_context, classification_confidence,
                files_loaded, files_rejected, rejection_reasons, token_count, privacy_level,
                cross_compartment, model_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                query,
                arena,
                confidence,
                json.dumps([item.id for item in loaded]),
                json.dumps([item.id for item in rejected]),
                json.dumps([item.reason for item in rejected]),
                sum(approx_word_count(item.summary) for item in loaded),
                "mixed" if any(item.reason == "compartment_blocked" for item in rejected) else "normal",
                int(bool(rejected)),
                None,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        pass
