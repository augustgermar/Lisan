"""Retrieval layer internals: candidate generation, scoring, and fusion.

Each layer ranks candidates independently — SQL metadata, FTS5 lexical,
vector semantic — and the layers are fused with RRF. Signals stay
separated by design: the blended score ranks, but no high-consequence
operation is licensed by the blend alone. Visibility here is about
quarantine and structural blocks only; retrieval is never gated on
sensitivity — disclosure judgments belong at the (future) outbound
boundary, not between the agent and its own memory.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import load_markdown
from ..paths import sqlite_path, vault_root
from .common import iter_markdown_files
from .tracing import record_retrieval_result
from .vector_store import VectorScorer, build_query_scorer
from .primer_index import assistant_display_name, principal_name
from ..utils import approx_word_count, today_iso


DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "physical": {
        "body", "sleep", "health", "fitness", "exercise", "pain", "energy",
        "doctor", "appointment", "medication", "therapy", "diagnosis", "symptom",
        "clinic", "hospital", "physical", "weight", "diet", "eating", "tired",
        "sick", "injury", "recovery", "surgery", "prescription", "checkup",
        "dr.", "dr ", "physician", "therapist",
    },
    "environmental": {
        "home", "room", "space", "environment", "apartment", "house",
        "rent", "lease", "move", "moving", "neighborhood", "city", "place",
        "office", "commute", "landlord", "furniture", "clean", "declutter",
    },
    "financial": {
        "money", "budget", "cash", "expense", "income", "tax", "finance",
        "salary", "savings", "spending", "pay", "bill", "debt", "loan",
        "invest", "investment", "afford", "cost", "price", "payment", "raise",
        "bonus", "account", "bank", "mortgage", "insurance",
    },
    "relational": {
        "friend", "partner", "family", "relationship", "dating", "marriage",
        "son", "daughter", "brother", "sister", "mom", "dad", "mother", "father",
        "wife", "husband", "boyfriend", "girlfriend", "fiance", "colleague",
        "coworker", "neighbor", "cousin", "uncle", "aunt", "grandma", "grandpa",
        "breakup", "divorce", "engaged", "wedding", "baby", "pregnant",
    },
    "work": {
        "work", "job", "project", "team", "meeting", "deadline", "client",
        "boss", "manager", "promotion", "fired", "hired", "resign", "quit",
        "interview", "offer", "onboard", "sprint", "launch", "product",
        "colleague", "performance", "review", "feedback", "career",
    },
    "status": {"status", "reputation", "credibility", "resume", "linkedin", "profile"},
    "appearance": {"appearance", "look", "style", "dress", "hair", "clothes", "outfit", "presentation"},
    "competence": {"competence", "skill", "capable", "ability", "performance", "learn", "practice", "improve"},
    "social_presence": {"social", "presence", "network", "community", "visibility", "post", "audience", "followers"},
    "desirability": {"desirable", "attractive", "desired", "romantic", "dating", "attractive"},
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
    expanded: bool = False
    hop: int = 0
    expansion_source: str = ""
    expansion_path: str = ""
    expansion_reason: str = ""


@dataclass(slots=True)
class RetrievalResult:
    domain: str
    confidence: float
    loaded: list[RetrievalItem]
    direct_loaded: list[RetrievalItem]
    expanded_loaded: list[RetrievalItem]
    rejected: list[RetrievalItem]
    graph_blocked: list[RetrievalItem]
    prompt: str

    @property
    def arena(self) -> str:
        return self.domain


@dataclass(slots=True)
class _LayerCandidate:
    id: str
    score: float
    source: str







def _retrieval_fusion_settings(config: dict[str, Any]) -> dict[str, Any]:
    retrieval_cfg = dict(config.get("retrieval", {}) or {})
    fusion_cfg = dict(retrieval_cfg.get("fusion", {}) or {})
    return {
        "enabled": bool(fusion_cfg.get("enabled", True)),
        "method": str(fusion_cfg.get("method", "rrf") or "rrf"),
        "rrf_k": int(fusion_cfg.get("rrf_k", 60) or 60),
        "per_layer_limit": int(fusion_cfg.get("per_layer_limit", 30) or 30),
        "fused_limit": int(fusion_cfg.get("fused_limit", 20) or 20),
        "recency_decay_days": int(retrieval_cfg.get("recency_decay_days", 365) or 365),
        # Reply-query pass: the assistant's PREVIOUS reply runs as its own
        # FTS + vector queries at a smaller budget — never concatenated onto
        # the user's message, which would average two speakers' retrieval
        # intents. Surfaces the threads the assistant is actively developing
        # that the user references without naming ("do the second one").
        "reply_query_enabled": bool(fusion_cfg.get("reply_query_enabled", True)),
        "reply_query_limit": int(fusion_cfg.get("reply_query_limit", 10) or 10),
        "reply_query_min_words": int(fusion_cfg.get("reply_query_min_words", 6) or 6),
        # Serendipity: reserve this many fused slots for weighted picks from
        # the mid-tier (30th-70th percentile) of the ranked pool — the same
        # records must not always load. Seeded from the query text, so the
        # same query gets the same "random" pick: reproducible serendipity.
        "serendipity_slots": int(fusion_cfg.get("serendipity_slots", 1) or 0),
    }


def _collect_rejected_items(
    file_rows: list[sqlite3.Row],
    *,
    active_contexts: set[str],
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
) -> list[RetrievalItem]:
    rejected: list[RetrievalItem] = []
    for row in file_rows:
        blocked_reason = _visibility_block_reason(
            row,
            active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
        )
        if blocked_reason is None:
            continue
        rejected.append(
            RetrievalItem(
                id=str(row["id"]),
                type=str(row["type"]),
                path=str(row["path"]),
                summary=str(row["summary"]),
                score=0.0,
                reason=blocked_reason,
            )
        )
    return rejected


def _sql_ranked_candidates(
    file_rows: list[sqlite3.Row],
    arena: str,
    *,
    query: str,
    active_contexts: set[str],
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
    limit: int,
    recency_decay_days: int = 365,
) -> list[_LayerCandidate]:
    candidates: list[_LayerCandidate] = []
    today = date.today()
    for row in file_rows:
        if _visibility_block_reason(
            row,
            active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
        ) is not None:
            continue
        score = _sql_metadata_score(row, arena, query, today=today, recency_decay_days=recency_decay_days)
        candidates.append(_LayerCandidate(id=str(row["id"]), score=score, source="sql"))
    return _truncate_layer_candidates(candidates, limit)


def _fts_ranked_candidates(
    conn: sqlite3.Connection,
    *,
    file_rows: list[sqlite3.Row],
    query: str,
    active_contexts: set[str],
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
    limit: int,
) -> tuple[list[_LayerCandidate], str]:
    query_terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query.lower()) if len(term) > 2]
    if not query_terms:
        return [], "bm25"

    fts_query = " OR ".join(f'"{_fts_escape(term)}"' for term in query_terms)
    try:
        rows = conn.execute(
            "SELECT id, bm25(files_fts) AS rank FROM files_fts WHERE files_fts MATCH ? ORDER BY rank ASC LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        if rows:
            candidates: list[_LayerCandidate] = []
            for row in rows:
                candidate_id = str(row["id"] or "")
                file_row = _row_by_id(file_rows, candidate_id)
                if file_row is None:
                    continue
                if _visibility_block_reason(
                    file_row,
                    active_contexts,
                    quarantined_artifact_ids=quarantined_artifact_ids,
                    quarantined_batch_ids=quarantined_batch_ids,
                    include_quarantined=include_quarantined,
                ) is not None:
                    continue
                rank_value = row["rank"]
                try:
                    score = -float(rank_value)
                except Exception:
                    score = 0.0
                candidates.append(_LayerCandidate(id=candidate_id, score=score, source="fts_bm25"))
            return _truncate_layer_candidates(candidates, limit), "bm25"
    except sqlite3.Error:
        pass

    match_rows: list[sqlite3.Row] = []
    try:
        match_rows = conn.execute(
            "SELECT id, summary, content FROM files_fts WHERE files_fts MATCH ?",
            (fts_query,),
        ).fetchall()
    except sqlite3.Error:
        match_rows = []

    if match_rows:
        candidates = []
        for row in match_rows:
            candidate_id = str(row["id"] or "")
            file_row = _row_by_id(file_rows, candidate_id)
            if file_row is None:
                continue
            if _visibility_block_reason(
                file_row,
                active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
            ) is not None:
                continue
            text = f"{row['summary'] or ''}\n{row['content'] or ''}"
            score = _term_count_score(text, query_terms)
            candidates.append(_LayerCandidate(id=candidate_id, score=score, source="fts_match_fallback"))
        return _truncate_layer_candidates(candidates, limit), "match_fallback"

    candidates = []
    for row in file_rows:
        if _visibility_block_reason(
            row,
            active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
        ) is not None:
            continue
        score = _term_count_score(_metadata_haystack(row), query_terms)
        if score <= 0:
            continue
        candidates.append(_LayerCandidate(id=str(row["id"]), score=score, source="fts_match_fallback"))
    return _truncate_layer_candidates(candidates, limit), "match_fallback"


def _vector_ranked_candidates(
    file_rows: list[sqlite3.Row],
    *,
    vector_scorer: VectorScorer,
    active_contexts: set[str],
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
    limit: int,
) -> list[_LayerCandidate]:
    # When the scorer is inactive (query skipped, or empty index) there is no
    # vector signal — return nothing so RRF runs on SQL+FTS only rather than
    # padding the fusion with zero-score ties.
    if not vector_scorer.active:
        return []
    candidates: list[_LayerCandidate] = []
    for row in file_rows:
        if _visibility_block_reason(
            row,
            active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
        ) is not None:
            continue
        score = vector_scorer.score(row["id"])
        if score <= 0:
            continue
        candidates.append(_LayerCandidate(id=str(row["id"]), score=score, source="vector"))
    return _truncate_layer_candidates(candidates, limit)


def _fuse_ranked_candidates(
    *,
    rows_by_id: dict[str, sqlite3.Row],
    sql_candidates: list[_LayerCandidate],
    fts_candidates: list[_LayerCandidate],
    vector_candidates: list[_LayerCandidate],
    rrf_k: int,
    fused_limit: int,
    extra_candidate_lists: list[list[_LayerCandidate]] | None = None,
    serendipity_slots: int = 0,
    serendipity_seed: str = "",
) -> tuple[list[RetrievalItem], dict[str, Any]]:
    candidate_lists = [sql_candidates, fts_candidates, vector_candidates]
    candidate_lists.extend(extra_candidate_lists or [])
    rrf_scores: dict[str, float] = defaultdict(float)
    source_order: dict[str, list[str]] = defaultdict(list)
    for candidates in candidate_lists:
        for rank, candidate in enumerate(candidates, start=1):
            rrf_scores[candidate.id] += 1.0 / (rrf_k + rank)
            if candidate.source not in source_order[candidate.id]:
                source_order[candidate.id].append(candidate.source)

    fused_items: list[RetrievalItem] = []
    ranked_remainder: list[tuple[str, float]] = []
    for record_id, score in sorted(
        rrf_scores.items(),
        key=lambda item: (
            item[1],
            len(source_order.get(item[0], [])),
            int(any(source.startswith("fts") for source in source_order.get(item[0], []))),
            _type_boost(str(rows_by_id[item[0]]["type"])) if item[0] in rows_by_id else 0.0,
            str(rows_by_id[item[0]]["summary"]) if item[0] in rows_by_id else item[0],
        ),
        reverse=True,
    ):
        row = rows_by_id.get(record_id)
        if row is None:
            continue
        if len(fused_items) >= fused_limit:
            ranked_remainder.append((record_id, score))
            continue
        sources = source_order.get(record_id, [])
        fused_items.append(
            _item_from_row(
                row,
                score=round(score, 6),
                reason=f"rrf:{'+'.join(sources)}" if sources else "rrf",
            )
        )

    fused_items = _apply_serendipity(
        fused_items,
        ranked_remainder=ranked_remainder,
        rows_by_id=rows_by_id,
        slots=serendipity_slots,
        seed=serendipity_seed,
    )

    stats = {
        "sql_candidate_count": len(sql_candidates),
        "fts_candidate_count": len(fts_candidates),
        "vector_candidate_count": len(vector_candidates),
        "fused_candidate_count": len(fused_items),
        "overlap_count": sum(1 for sources in source_order.values() if len(set(sources)) > 1),
        "source_order": source_order,
    }
    return fused_items, stats


def _apply_serendipity(
    fused_items: list[RetrievalItem],
    *,
    ranked_remainder: list[tuple[str, float]],
    rows_by_id: dict[str, sqlite3.Row],
    slots: int,
    seed: str,
) -> list[RetrievalItem]:
    """Swap the tail of the fused set for weighted picks from the mid-tier
    (30th-70th percentile) of the unselected remainder. Seeded from the query
    text: the same query reproduces the same picks, so retrieval stays
    reproducible while different queries stop always loading the same set."""
    if slots <= 0 or not ranked_remainder or len(fused_items) <= slots:
        return fused_items
    import random

    p30 = int(len(ranked_remainder) * 0.3)
    p70 = int(len(ranked_remainder) * 0.7)
    band = ranked_remainder[p30:p70] or ranked_remainder[:1]
    rng = random.Random(seed or "serendipity")
    picks: list[tuple[str, float]] = []
    pool = list(band)
    for _ in range(min(slots, len(pool))):
        weights = [max(score, 1e-9) for _, score in pool]
        chosen = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        picks.append(pool.pop(chosen))
    if not picks:
        return fused_items
    kept = fused_items[: len(fused_items) - len(picks)]
    for record_id, score in picks:
        row = rows_by_id.get(record_id)
        if row is None:
            continue
        kept.append(_item_from_row(row, score=round(score, 6), reason="serendipity"))
    return kept


def _demote_graph_neighbors(
    direct_loaded: list[RetrievalItem],
    *,
    rows_by_id: dict[str, sqlite3.Row],
    link_rows: list[sqlite3.Row],
    source_order: dict[str, list[str]],
) -> list[RetrievalItem]:
    if len(direct_loaded) < 2:
        return direct_loaded
    edges_by_source = _build_graph_edges(rows_by_id, link_rows)
    kept: list[RetrievalItem] = []
    for item in direct_loaded:
        source_count = len(set(source_order.get(item.id, [])))
        if source_count >= 3:
            kept.append(item)
            continue
        linked_from_kept = False
        for kept_item in kept:
            for edge in edges_by_source.get(kept_item.id, []):
                if edge.target_id == item.id:
                    linked_from_kept = True
                    break
            if linked_from_kept:
                break
        if linked_from_kept:
            continue
        kept.append(item)
    return kept


def _item_from_row(row: sqlite3.Row, *, score: float, reason: str) -> RetrievalItem:
    return RetrievalItem(
        id=str(row["id"]),
        type=str(row["type"]),
        path=str(row["path"]),
        summary=str(row["summary"]),
        score=round(score, 3),
        reason=reason,
    )


def _truncate_layer_candidates(candidates: list[_LayerCandidate], limit: int) -> list[_LayerCandidate]:
    candidates.sort(key=lambda candidate: (candidate.score, candidate.id), reverse=True)
    return candidates[:limit]


def _row_by_id(file_rows: list[sqlite3.Row], record_id: str) -> sqlite3.Row | None:
    for row in file_rows:
        if str(row["id"]) == record_id:
            return row
    return None


def _term_count_score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _sql_metadata_score(
    row: sqlite3.Row,
    arena: str,
    query: str,
    today: date | None = None,
    recency_decay_days: int = 365,
) -> float:
    score = 0.0
    if row["domain_primary"] == arena or row["domain_primary"] == "cross_arena":
        score += 2.0
    score += _type_boost(str(row["type"]))
    try:
        updated_str = str(row["updated"] or "").strip()
        if updated_str and today is not None:
            days_old = (today - date.fromisoformat(updated_str)).days
            if days_old >= 0:
                score += max(0.0, 1.0 - (days_old / recency_decay_days))
    except (ValueError, TypeError):
        pass
    lowered = query.lower()
    query_terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", lowered) if len(term) > 2]
    haystack = _metadata_haystack(row)
    if query_terms and any(term in haystack for term in query_terms):
        score += 1.2
    if row["type"] == "evidence":
        actors = {item.lower() for item in _json_list(row["actors"])}
        if any(term in actors for term in query_terms):
            score += 1.0
        if any(term == str(row["source_type"]).lower() for term in query_terms):
            score += 0.7
    elif row["type"] == "claim":
        if any(term == str(row["claim_class"]).lower() for term in query_terms):
            score += 0.8
        if any(term == str(row["status"]).lower() for term in query_terms):
            score += 0.6
    elif row["type"] == "pattern":
        if any(term == str(row["pattern_type"]).lower() for term in query_terms):
            score += 0.8
    elif row["type"] == "artifact":
        if any(term == str(row["file_name"] or "").lower() for term in query_terms):
            score += 0.8
        if any(term in str(row["source_path"] or "").lower() for term in query_terms):
            score += 0.8
    if str(row["status"]) == "active":
        score += 0.5
    elif str(row["status"]) in {"confirmed", "disputed"}:
        score += 0.2
    if str(row["significance"]) == "high":
        score += 0.7
    confidence_score = row["confidence_score"] if "confidence_score" in row.keys() else None
    if isinstance(confidence_score, (int, float)):
        score += float(confidence_score) * 0.3
    return score


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
    *,
    vector_scorer: VectorScorer,
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
) -> RetrievalItem | None:
    blocked_reason = _visibility_block_reason(
        row,
        active_contexts,
        quarantined_artifact_ids=quarantined_artifact_ids,
        quarantined_batch_ids=quarantined_batch_ids,
        include_quarantined=include_quarantined,
    )
    if blocked_reason is not None:
        return RetrievalItem(id=str(row["id"]), type=str(row["type"]), path=str(row["path"]), summary=str(row["summary"]), score=0.0, reason=blocked_reason)

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
    elif row["type"] == "artifact":
        score += 1.8
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
    elif row["type"] == "artifact":
        if any(term == str(row["file_name"] or "").lower() for term in query_terms):
            score += 1.5
            reasons.append("file_name")
        if any(term in str(row["source_path"] or "").lower() for term in query_terms):
            score += 1.5
            reasons.append("source_path")
        if any(term == str(row["artifact_hash"] or "").lower() for term in query_terms):
            score += 1.0
            reasons.append("artifact_hash")

    if str(row["id"]) in fts_ids:
        score += 2.0
        reasons.append("fts")

    vector_score = vector_scorer.score(row["id"])
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


def _unique_items(items: list[RetrievalItem]) -> list[RetrievalItem]:
    seen: set[str] = set()
    unique: list[RetrievalItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique


def _is_blocked_visibility_reason(reason: str) -> bool:
    return reason == "quarantined"


def _quarantine_sets(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    quarantined_artifacts: set[str] = set()
    quarantined_batches: set[str] = set()
    try:
        batch_rows = conn.execute("SELECT id FROM ingestion_batches WHERE status = 'quarantined'").fetchall()
        quarantined_batches = {str(row["id"]) for row in batch_rows if str(row["id"] or "")}
    except sqlite3.Error:
        quarantined_batches = set()
    try:
        artifact_rows = conn.execute(
            """
            SELECT id, batch_id, ingestion_status, status
            FROM files
            WHERE type = 'artifact'
            """
        ).fetchall()
        for row in artifact_rows:
            artifact_id = str(row["id"] or "")
            batch_id = str(row["batch_id"] or "")
            ingestion_status = str(row["ingestion_status"] or "")
            status = str(row["status"] or "")
            if artifact_id and (
                batch_id in quarantined_batches
                or ingestion_status == "quarantined"
                or status == "quarantined"
            ):
                quarantined_artifacts.add(artifact_id)
    except sqlite3.Error:
        quarantined_artifacts = set()
    return quarantined_artifacts, quarantined_batches


def _visibility_block_reason(
    row: sqlite3.Row,
    active_contexts: set[str],
    *,
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
) -> str | None:
    if include_quarantined:
        return None
    row_id = str(row["id"])
    row_type = str(row["type"])
    row_batch_id = str(row["batch_id"] or "")
    artifact_ref = str(row["artifact_ref"] or "")
    if row_type == "artifact" and (
        str(row["ingestion_status"] or "") == "quarantined"
        or str(row["status"] or "") == "quarantined"
        or row_id in quarantined_artifact_ids
        or row_batch_id in quarantined_batch_ids
    ):
        return "quarantined"
    if row_type in {"evidence", "claim", "pattern", "skeptical_review", "episode", "decision", "open_loop", "state", "report"}:
        if row_batch_id in quarantined_batch_ids:
            return "quarantined"
        if artifact_ref and artifact_ref in quarantined_artifact_ids:
            return "quarantined"
    # NOTE: Privacy/disclosure gating intentionally does NOT happen here.
    # Internal retrieval has full access to memory (the system must reason over
    # everything it knows). Disclosure control is relational and happens at the
    # EXTERNAL communication boundary — see execution-layer-spec.md
    # (disclosure gate). Do not re-add context/compartment blocking to internal
    # retrieval.
    return None


def _type_boost(record_type: str) -> float:
    boosts = {
        "evidence": 3.0,
        "claim": 2.6,
        "pattern": 2.4,
        "skeptical_review": 2.2,
        "artifact": 2.0,
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


def _fts_escape(term: str) -> str:
    return term.replace('"', "").replace("'", "")


def _metadata_haystack(row: sqlite3.Row) -> str:
    parts = [
        str(row["summary"]),
        str(row["id"]),
        str(row["path"]),
        str(row["source_type"] or ""),
        str(row["source_path"] or ""),
        str(row["file_name"] or ""),
        str(row["file_ext"] or ""),
        str(row["mime_type"] or ""),
        str(row["ingestion_status"] or ""),
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
    for field in ["actors", "compartments", "linked_claims", "linked_episodes", "supporting_evidence", "contradicting_evidence", "linked_patterns", "reasoning_errors", "supporting_records", "counterexamples", "alternative_explanations", "predictions", "linked_evidence", "parse_errors"]:
        parts.extend(_json_list(row[field]) if field in row.keys() else [])
    return "\n".join(parts).lower()


from .retrieval_graph import (
    _GraphEdge,
    _expand_graph,
    _build_graph_edges,
    _graph_relation_fields,
    _graph_candidates,
    _sort_graph_candidates,
    _is_allowed_graph_edge,
    _target_type,
    _graph_expansion_reason,
    _graph_score_reason,
    _graph_relation_bonus,
    _render_expansion_path,
    _cross_domain_expansion_allowed,
    _path_contains_bridge_pattern,
    _dreamer_coupled_pairs,
    _explicit_query_domains,
)
