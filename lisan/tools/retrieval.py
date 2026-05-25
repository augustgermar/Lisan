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
from ..utils import approx_word_count, hash_embedding


DOMAIN_KEYWORDS: dict[str, set[str]] = {
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
    domain: str
    confidence: float
    loaded: list[RetrievalItem]
    rejected: list[RetrievalItem]
    prompt: str

    @property
    def arena(self) -> str:
        return self.domain


def assemble_context(
    query: str,
    domain: str | None = None,
    arena: str | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
    conversation_id: str | None = None,
) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    result = retrieve_context(query=query, domain=domain, arena=arena, vault=vault, db_path=db_path, conversation_id=conversation_id)
    sections: list[str] = ["# Assembled Context", ""]
    sections.append(f"domain: {result.domain}")
    sections.append(f"domain_confidence: {result.confidence:.2f}")
    sections.append("")
    sections.append(f"query: {query}")
    sections.append("")

    for rel in ["primer/identity.md", "primer/operating-style.md", "primer/current-brief.md"]:
        path = vault / rel
        if path.exists():
            sections.append(f"## {rel}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    groups = {
        "evidence": [item for item in result.loaded if item.type == "evidence"],
        "claim": [item for item in result.loaded if item.type == "claim"],
        "pattern": [item for item in result.loaded if item.type == "pattern"],
        "skeptical_review": [item for item in result.loaded if item.type == "skeptical_review"],
        "episode": [item for item in result.loaded if item.type == "episode"],
        "report": [item for item in result.loaded if item.type == "report"],
        "state": [item for item in result.loaded if item.type == "state"],
        "other": [item for item in result.loaded if item.type not in {"evidence", "claim", "pattern", "skeptical_review", "episode", "report", "state"}],
    }

    section_order = [
        ("## Evidence", groups["evidence"]),
        ("## Claims", groups["claim"]),
        ("## Patterns", groups["pattern"]),
        ("## Skeptical Reviews", groups["skeptical_review"]),
        ("## Episodes", groups["episode"]),
        ("## Dreamer Summaries", groups["report"]),
        ("## State", groups["state"]),
        ("## Relevant Records", groups["other"]),
    ]
    for heading, items in section_order:
        sections.append(heading)
        if not items:
            sections.append("- None")
            sections.append("")
            continue
        for item in items:
            path = vault / item.path
            details = _format_item_detail(item, path)
            sections.append(details)
        sections.append("")

    if result.rejected:
        sections.append("## Rejected By Compartment")
        for item in result.rejected:
            sections.append(f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}")
        sections.append("")

    contradiction_notes = _load_relevant_contradictions(vault, query)
    if contradiction_notes:
        sections.append("## Active Contradictions")
        sections.append("NOTE: The following unresolved contradictions are relevant to this context.")
        for note in contradiction_notes:
            sections.append(f"- {note}")
        sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def retrieve_context(
    query: str,
    domain: str | None = None,
    arena: str | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
    conversation_id: str | None = None,
) -> RetrievalResult:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    inferred_domain, confidence = _infer_domain(query, domain or arena)
    active_contexts = _active_contexts(inferred_domain, query)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        fts_ids = _fts_candidate_ids(conn, query)
        file_rows = conn.execute("SELECT * FROM files").fetchall()
        all_items = [_score_row(row, query, inferred_domain, vault, active_contexts, fts_ids) for row in file_rows]
        loaded = [item for item in all_items if item is not None]
        loaded.sort(key=lambda item: (_type_boost(item.type), item.score, item.summary), reverse=True)
        loaded = loaded[:15]
        loaded_ids = {item.id for item in loaded}
        rejected = [item for item in all_items if item is not None and item.id not in loaded_ids and item.reason.startswith("compartment")]
        _log_retrieval(
            conn,
            conversation_id=conversation_id,
            query=query,
            arena=inferred_domain,
            confidence=confidence,
            loaded=loaded,
            rejected=rejected,
        )
        return RetrievalResult(domain=inferred_domain, confidence=confidence, loaded=loaded, rejected=rejected, prompt=query)
    finally:
        conn.close()


def _infer_domain(query: str, explicit: str | None) -> tuple[str, float]:
    if explicit:
        return explicit, 1.0
    lowered = query.lower()
    best = "cross_arena"
    best_score = 0
    for domain_name, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score > best_score:
            best = domain_name
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

    def _blocked(reason: str = "compartment_blocked") -> RetrievalItem:
        return RetrievalItem(id=str(row["id"]), type=str(row["type"]), path=str(row["path"]), summary=str(row["summary"]), score=0.0, reason=reason)

    # File's blocked_contexts are contexts where it must NOT appear
    if blocked_contexts and set(blocked_contexts).intersection(active_contexts):
        return _blocked()

    # File's allowed_contexts restrict which contexts may see it
    if allowed_contexts and "all" not in allowed_contexts:
        if not set(allowed_contexts).intersection(active_contexts):
            return _blocked()

    # Sensitive compartments (non-life-domain privacy boundaries) restrict independently of allowed_contexts
    _SENSITIVE = {"legal", "health", "children"}
    sensitive = _SENSITIVE.intersection(compartments)
    if sensitive and not sensitive.intersection(active_contexts):
        return _blocked()

    score = 0.0
    reasons: list[str] = []
    lowered = query.lower()
    summary = str(row["summary"])
    haystack = _metadata_haystack(row)

    if row["domain_primary"] == arena or row["domain_primary"] == "cross_arena":
        score += 2.0
        reasons.append("domain_match")
    score += _type_boost(str(row["type"]))
    if row["type"] == "evidence":
        score += 1.5
    elif row["type"] == "claim":
        score += 1.2
    elif row["type"] == "pattern":
        score += 1.4
    elif row["type"] == "skeptical_review":
        score += 1.1
    elif row["type"] == "report":
        score += 1.0
    elif row["type"] == "episode":
        score += 0.9

    query_terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", lowered) if len(term) > 2]
    if query_terms and any(term in haystack for term in query_terms):
        score += 2.5
        reasons.append("text_match")
    if row["type"] == "evidence":
        actors = {item.lower() for item in _json_list(row["actors"])}
        if any(term in actors for term in query_terms):
            score += 1.5
            reasons.append("actor_match")
        if any(term == str(row["source_type"]).lower() for term in query_terms):
            score += 1.0
            reasons.append("source_type")
    elif row["type"] == "claim":
        if any(term == str(row["claim_class"]).lower() for term in query_terms):
            score += 1.0
            reasons.append("claim_class")
        if any(term == str(row["status"]).lower() for term in query_terms):
            score += 0.8
            reasons.append("claim_status")
    elif row["type"] == "pattern":
        if any(term == str(row["pattern_type"]).lower() for term in query_terms):
            score += 1.1
            reasons.append("pattern_type")
        if any(term in {"pattern", "hypothesis", "loop", "trigger", "gap"} for term in query_terms):
            score += 0.5
            reasons.append("pattern_query")
    elif row["type"] == "skeptical_review":
        if any(term == str(row["reviewed_record_id"]).lower() for term in query_terms):
            score += 1.0
            reasons.append("review_link")

    if str(row["id"]) in fts_ids:
        score += 2.0
        reasons.append("fts")

    vector_score = _vector_score(query, row["id"], vault)
    if vector_score:
        score += vector_score * 2.0
        reasons.append("vector")

    if str(row["status"]) == "active":
        score += 0.5
    elif str(row["status"]) in {"confirmed", "disputed"}:
        score += 0.2
    if str(row["significance"]) == "high":
        score += 0.7
    confidence_score = row["confidence_score"] if "confidence_score" in row.keys() else None
    if isinstance(confidence_score, (int, float)):
        score += float(confidence_score) * 0.3

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


def _type_boost(record_type: str) -> float:
    boosts = {
        "evidence": 3.0,
        "claim": 2.6,
        "pattern": 2.4,
        "skeptical_review": 2.2,
        "report": 1.8,
        "contradiction_log": 1.7,
        "episode": 1.4,
        "decision": 1.2,
        "open_loop": 1.1,
        "state": 1.0,
        "entity": 0.8,
        "knowledge": 0.7,
    }
    return boosts.get(record_type, 0.0)


def _metadata_haystack(row: sqlite3.Row) -> str:
    parts = [
        str(row["summary"]),
        str(row["id"]),
        str(row["path"]),
        str(row["source_type"] or ""),
        str(row["arena"] or ""),
        str(row["pattern_type"] or ""),
        str(row["hypothesis"] or ""),
        str(row["claim_class"] or ""),
        str(row["owner"] or ""),
        str(row["status"] or ""),
        str(row["risk"] or ""),
        str(row["recommended_action"] or ""),
        str(row["reviewed_record_id"] or ""),
        str(row["reviewed_record_type"] or ""),
    ]
    for field in ["actors", "compartments", "linked_claims", "linked_episodes", "supporting_evidence", "contradicting_evidence", "linked_patterns", "reasoning_errors", "supporting_records", "counterexamples", "alternative_explanations", "predictions"]:
        parts.extend(_json_list(row[field]) if field in row.keys() else [])
    return "\n".join(parts).lower()


def _format_item_detail(item: RetrievalItem, path: Path) -> str:
    if not path.exists():
        return f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}"
    try:
        doc = load_markdown(path)
    except Exception:
        return f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}"
    fm = doc.frontmatter
    if item.type == "evidence":
        return (
            f"### `{item.id}`\n"
            f"- summary: {fm.get('summary', item.summary)}\n"
            f"- source_type: {fm.get('source_type', 'unknown')}\n"
            f"- actors: {', '.join(_json_list(fm.get('actors'))) or 'none'}\n"
            f"- reliability: {fm.get('reliability', 'unknown')}\n"
            f"- observed_facts: {', '.join(_json_list(fm.get('observed_facts'))) or 'none'}\n"
            f"- link: `{item.path}`\n"
            f"- reason: {item.reason}"
        )
    if item.type == "claim":
        return (
            f"### `{item.id}`\n"
            f"- claim_text: {fm.get('claim_text', item.summary)}\n"
            f"- class: {fm.get('claim_class', 'unknown')}\n"
            f"- owner: {fm.get('owner', 'unknown')}\n"
            f"- status: {fm.get('status', 'unknown')}\n"
            f"- confidence: {fm.get('confidence', 'unknown')}\n"
            f"- supporting_evidence: {', '.join(_json_list(fm.get('supporting_evidence'))) or 'none'}\n"
            f"- contradicting_evidence: {', '.join(_json_list(fm.get('contradicting_evidence'))) or 'none'}\n"
            f"- link: `{item.path}`\n"
            f"- reason: {item.reason}"
        )
    if item.type == "pattern":
        return (
            f"### `{item.id}`\n"
            f"- hypothesis: {fm.get('hypothesis', item.summary)}\n"
            f"- pattern_type: {fm.get('pattern_type', 'unknown')}\n"
            f"- confidence: {fm.get('confidence', 'unknown')}\n"
            f"- supporting_records: {', '.join(_json_list(fm.get('supporting_records'))) or 'none'}\n"
            f"- counterexamples: {', '.join(_json_list(fm.get('counterexamples'))) or 'none'}\n"
            f"- alternative_explanations: {', '.join(_json_list(fm.get('alternative_explanations'))) or 'none'}\n"
            f"- predictions: {', '.join(_json_list(fm.get('predictions'))) or 'none'}\n"
            f"- evidence_needed: {', '.join(_json_list(fm.get('evidence_needed'))) or 'none'}\n"
            f"- link: `{item.path}`\n"
            f"- reason: {item.reason}"
        )
    if item.type == "skeptical_review":
        return (
            f"### `{item.id}`\n"
            f"- summary: {fm.get('summary', item.summary)}\n"
            f"- reviewed_record_id: {fm.get('reviewed_record_id', 'unknown')}\n"
            f"- risk: {fm.get('risk', 'unknown')}\n"
            f"- recommended_action: {fm.get('recommended_action', 'unknown')}\n"
            f"- reasoning_errors: {', '.join(_json_list(fm.get('reasoning_errors'))) or 'none'}\n"
            f"- link: `{item.path}`\n"
            f"- reason: {item.reason}"
        )
    if item.type == "report":
        return (
            f"### `{item.id}`\n"
            f"- summary: {fm.get('summary', item.summary)}\n"
            f"- task: {fm.get('task', 'unknown')}\n"
            f"- link: `{item.path}`\n"
            f"- reason: {item.reason}"
        )
    if item.type == "episode":
        return f"### `{item.id}`\n- summary: {fm.get('summary', item.summary)}\n- link: `{item.path}`\n- reason: {item.reason}"
    return f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}"


def _vector_score(query: str, file_id: str, vault: Path) -> float:
    query_vec = hash_embedding(query)
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
                conversation_id, user_query, domain_context, classification_confidence,
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


def _load_relevant_contradictions(vault: Path, query: str) -> list[str]:
    """Return summary lines for active (unresolved) contradiction files relevant to this query."""
    contradictions_dir = vault / "contradictions"
    if not contradictions_dir.exists():
        return []
    query_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query) if len(t) > 3}
    notes: list[str] = []
    from datetime import date
    for path in sorted(contradictions_dir.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("status", "active")) in ("resolved", "archived"):
            continue
        summary = str(doc.frontmatter.get("summary") or "").lower()
        body_snippet = path.read_text(encoding="utf-8")[:800].lower()
        haystack = summary + " " + body_snippet
        if not query_terms or any(term in haystack for term in query_terms):
            created = doc.frontmatter.get("created")
            age = ""
            if created:
                try:
                    age = f" ({(date.today() - date.fromisoformat(str(created))).days}d old)"
                except ValueError:
                    pass
            notes.append(f"`{path.name}`{age}: {doc.frontmatter.get('summary', 'unresolved contradiction')}")
    return notes[:5]
