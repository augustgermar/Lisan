from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..providers.embeddings import EmbeddingProvider
from ..tools.vector_store import cosine_similarity
from ..utils import hash_embedding
from .epistemic import listify
from ..config import load_config


_TOKEN_REPLACEMENTS = str.maketrans({"-": " ", "_": " "})


@dataclass(slots=True)
class ResolutionResult:
    candidate: dict[str, Any] | None
    confidence: float
    score: float
    method: str


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().translate(_TOKEN_REPLACEMENTS).split())


def tokenize_text(value: Any) -> set[str]:
    import re

    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]+", normalize_text(value))
        if len(token) > 2
    }


def candidate_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in (
        "name",
        "canonical_name",
        "title",
        "claim_text",
        "summary",
        "next_action",
        "hypothesis",
        "summary_text",
    ):
        value = str(candidate.get(field) or "").strip()
        if value:
            parts.append(value)
    for field in (
        "aliases",
        "links",
        "supporting_evidence",
        "contradicting_evidence",
        "linked_claims",
        "linked_episodes",
        "supporting_records",
        "counterexamples",
        "alternative_explanations",
        "recent_summaries",
    ):
        parts.extend(listify(candidate.get(field)))
    for field in ("domain_primary", "domain_secondary", "arena", "arena_primary", "arena_secondary", "status", "owner", "priority", "kind", "subtype", "disclosure"):
        value = candidate.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)


def candidate_keys(candidate: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("id", "name", "canonical_name", "title", "claim_text", "summary", "next_action", "hypothesis"):
        value = str(candidate.get(field) or "").strip()
        if value:
            keys.add(normalize_text(value))
    for field in ("aliases", "links", "supporting_evidence", "contradicting_evidence"):
        for item in listify(candidate.get(field)):
            keys.add(normalize_text(item))
    return {key for key in keys if key}


def _lexical_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens)
    union = len(query_tokens | candidate_tokens)
    return overlap / union if union else 0.0


def _semantic_score(query: str, candidate_text_value: str, provider: EmbeddingProvider) -> float:
    query_vec = provider.embed_text(query)
    cand_vec = provider.embed_text(candidate_text_value)
    if not query_vec or not cand_vec:
        return 0.0
    return max(0.0, cosine_similarity(query_vec, cand_vec))


def _normalized_confidence(score: float) -> float:
    return max(0.0, min(0.99, score))


def resolve_reference(
    new_neighborhood: str,
    candidates: list[dict[str, Any]],
    vault: Path | None = None,
) -> ResolutionResult:
    """Resolve a new mention against candidate records using deterministic
    matching first and contextual scoring for the residue.

    Returns the best candidate and a confidence in [0.0, 0.99]. A ``None``
    candidate with low confidence means the resolver could not distinguish the
    options safely.
    """
    if not candidates:
        return ResolutionResult(candidate=None, confidence=0.0, score=0.0, method="none")

    query = normalize_text(new_neighborhood)
    if not query:
        return ResolutionResult(candidate=None, confidence=0.0, score=0.0, method="empty")
    query_tokens = tokenize_text(query)
    provider = EmbeddingProvider(load_config())

    best_candidate: dict[str, Any] | None = None
    best_score = -1.0
    best_method = "context"

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        exact_keys = candidate_keys(candidate)
        candidate_text_value = candidate_text(candidate)
        candidate_tokens = tokenize_text(candidate_text_value)

        exact = 1.0 if query in exact_keys or any(key and (key in query or query in key) for key in exact_keys) else 0.0
        lexical = _lexical_score(query_tokens, candidate_tokens)
        semantic = _semantic_score(query, candidate_text_value, provider)

        score = max(exact, 0.40 * lexical + 0.55 * semantic)
        if exact > 0.0:
            score = max(score, 0.95)
            method = "deterministic"
        elif lexical >= 0.35 and semantic >= 0.2:
            method = "context"
        elif lexical >= 0.2 or semantic >= 0.4:
            method = "semantic"
        else:
            method = "residue"

        if score > best_score or (score == best_score and str(candidate.get("id") or "") < str(best_candidate.get("id") or "") if best_candidate else True):
            best_candidate = candidate
            best_score = score
            best_method = method

    if best_candidate is None:
        return ResolutionResult(candidate=None, confidence=0.0, score=0.0, method="none")

    confidence = _normalized_confidence(best_score)
    return ResolutionResult(candidate=best_candidate, confidence=confidence, score=best_score, method=best_method)


def resolution_action(confidence: float, *, load_bearing: bool) -> str:
    if confidence >= 0.8:
        return "bind"
    if confidence >= 0.6:
        return "provisional" if load_bearing else "bind"
    if load_bearing:
        return "clarify"
    return "defer"
