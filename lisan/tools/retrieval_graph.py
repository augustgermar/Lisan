from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..utils import approx_word_count, today_iso
from .retrieval_layers import (
    RetrievalItem,
    RetrievalResult,
    _LayerCandidate,
    _visibility_block_reason,
)

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
class _GraphEdge:
    source_id: str
    target_id: str
    relation: str


def _expand_graph(
    direct_loaded: list[RetrievalItem],
    rows_by_id: dict[str, sqlite3.Row],
    link_rows: list[sqlite3.Row],
    query: str,
    inferred_domain: str,
    vault: Path,
    active_contexts: set[str],
    quarantined_artifact_ids: set[str],
    quarantined_batch_ids: set[str],
    include_quarantined: bool,
    max_hops: int = 2,
    max_expanded_records: int = 5,
    max_cross_domain_records: int = 2,
) -> tuple[list[RetrievalItem], list[RetrievalItem]]:
    if not direct_loaded:
        return [], []

    edges_by_source = _build_graph_edges(rows_by_id, link_rows)
    query_domains = _explicit_query_domains(query)
    coupled_pairs = _dreamer_coupled_pairs(vault)
    direct_scores = {item.id: item.score for item in direct_loaded}
    expanded: list[RetrievalItem] = []
    blocked: list[RetrievalItem] = []
    visited = {item.id for item in direct_loaded}
    expanded_seen: set[str] = set()
    frontier = deque((item.id, 0, [item.id]) for item in direct_loaded)
    cross_domain_expanded = 0

    while frontier and len(expanded) < max_expanded_records:
        source_id, depth, path_ids = frontier.popleft()
        if depth >= max_hops:
            continue
        source_row = rows_by_id.get(source_id)
        if source_row is None:
            continue
        candidates = _graph_candidates(source_row, edges_by_source.get(source_id, []), rows_by_id)
        for edge in candidates:
            if len(expanded) >= max_expanded_records:
                break
            target_id = edge.target_id
            target_row = rows_by_id.get(target_id)
            if target_row is None:
                continue
            blocked_reason = _visibility_block_reason(
                target_row,
                active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
            )
            if blocked_reason is not None:
                blocked.append(
                    RetrievalItem(
                        id=target_id,
                        type=str(target_row["type"]),
                        path=str(target_row["path"]),
                        summary=str(target_row["summary"]),
                        score=0.0,
                        reason="graph_" + edge.relation + "_" + blocked_reason,
                        expanded=True,
                        hop=depth + 1,
                        expansion_source=source_id,
                        expansion_path=_render_expansion_path(path_ids + [target_id], rows_by_id),
                        expansion_reason=f"{edge.relation}:{blocked_reason}",
                    )
                )
                visited.add(target_id)
                continue
            allowed, reason = _cross_domain_expansion_allowed(
                query_domains=query_domains,
                source_id=source_id,
                target_id=target_id,
                path_ids=path_ids,
                rows_by_id=rows_by_id,
                coupled_pairs=coupled_pairs,
            )
            source_domain = str(source_row["domain_primary"] or source_row["arena"] or inferred_domain)
            target_domain = str(target_row["domain_primary"] or target_row["arena"] or inferred_domain)
            is_cross_domain = source_domain != target_domain and source_domain != "cross_arena" and target_domain != "cross_arena"
            if is_cross_domain and not allowed:
                blocked.append(
                    RetrievalItem(
                        id=target_id,
                        type=str(target_row["type"]),
                        path=str(target_row["path"]),
                        summary=str(target_row["summary"]),
                        score=0.0,
                        reason=f"graph_cross_domain_blocked:{reason}",
                        expanded=True,
                        hop=depth + 1,
                        expansion_source=source_id,
                        expansion_path=_render_expansion_path(path_ids + [target_id], rows_by_id),
                        expansion_reason=reason,
                    )
                )
                visited.add(target_id)
                continue
            if is_cross_domain:
                if cross_domain_expanded >= max_cross_domain_records:
                    blocked.append(
                        RetrievalItem(
                            id=target_id,
                            type=str(target_row["type"]),
                            path=str(target_row["path"]),
                            summary=str(target_row["summary"]),
                            score=0.0,
                            reason="graph_cross_domain_blocked:max_cross_domain_records",
                            expanded=True,
                            hop=depth + 1,
                            expansion_source=source_id,
                            expansion_path=_render_expansion_path(path_ids + [target_id], rows_by_id),
                            expansion_reason="max_cross_domain_records",
                        )
                    )
                    visited.add(target_id)
                    continue
                cross_domain_expanded += 1
            if target_id in expanded_seen:
                continue
            source_score = direct_scores.get(source_id, 0.0)
            relation_bonus = _graph_relation_bonus(edge.relation)
            expanded_item = RetrievalItem(
                id=target_id,
                type=str(target_row["type"]),
                path=str(target_row["path"]),
                summary=str(target_row["summary"]),
                score=round(max(0.1, source_score * 0.85 + relation_bonus - (0.08 * (depth + 1))), 3),
                reason=_graph_score_reason(edge.relation, source_row, target_row),
                expanded=True,
                hop=depth + 1,
                expansion_source=source_id,
                expansion_path=_render_expansion_path(path_ids + [target_id], rows_by_id),
                expansion_reason=_graph_expansion_reason(edge.relation, source_row, target_row),
            )
            expanded.append(expanded_item)
            expanded_seen.add(target_id)
            if target_id not in visited:
                visited.add(target_id)
                if depth + 1 < max_hops:
                    frontier.append((target_id, depth + 1, path_ids + [target_id]))
            if len(expanded) >= max_expanded_records:
                break
    return expanded, blocked


def _build_graph_edges(rows_by_id: dict[str, sqlite3.Row], link_rows: list[sqlite3.Row]) -> dict[str, list[_GraphEdge]]:
    edges: dict[str, list[_GraphEdge]] = defaultdict(list)
    for row in rows_by_id.values():
        source_id = str(row["id"])
        source_type = str(row["type"])
        for relation, field in _graph_relation_fields(source_type):
            for target_id in _json_list(row[field]) if field in row.keys() else []:
                edges[source_id].append(_GraphEdge(source_id=source_id, target_id=str(target_id), relation=relation))
    for row in link_rows:
        source_id = str(row["source_id"] or "")
        target_id = str(row["target_id"] or "")
        if not source_id or not target_id:
            continue
        relation = str(row["relationship_type"] or "links")
        edges[source_id].append(_GraphEdge(source_id=source_id, target_id=target_id, relation=relation))
    return edges


def _graph_relation_fields(source_type: str) -> list[tuple[str, str]]:
    mapping = {
        "artifact": [("linked_evidence", "linked_evidence"), ("linked_claims", "linked_claims")],
        "evidence": [("linked_claims", "linked_claims"), ("linked_episodes", "linked_episodes")],
        "claim": [("supporting_evidence", "supporting_evidence"), ("contradicting_evidence", "contradicting_evidence"), ("linked_patterns", "linked_patterns")],
        "pattern": [("supporting_records", "supporting_records"), ("counterexamples", "counterexamples")],
        "episode": [("entities", "entities"), ("evidence", "evidence"), ("claims", "claims")],
    }
    return mapping.get(source_type, [])


def _graph_candidates(source_row: sqlite3.Row, explicit_edges: list[_GraphEdge], rows_by_id: dict[str, sqlite3.Row]) -> list[_GraphEdge]:
    source_type = str(source_row["type"])
    candidates = list(explicit_edges)
    return _sort_graph_candidates(
        source_type,
        [edge for edge in candidates if _is_allowed_graph_edge(source_type, _target_type(rows_by_id, edge.target_id), edge.relation)],
    )


def _sort_graph_candidates(source_type: str, edges: list[_GraphEdge]) -> list[_GraphEdge]:
    priority = {
        "artifact": {"linked_evidence": 0, "linked_claims": 1, "artifact_provenance": 2, "links": 3},
        "evidence": {"linked_claims": 0, "linked_episodes": 1, "links": 2},
        "claim": {"supporting_evidence": 0, "contradicting_evidence": 1, "linked_patterns": 2, "links": 3},
        "pattern": {"supporting_records": 0, "counterexamples": 1, "links": 2},
        "episode": {"entities": 0, "evidence": 1, "claims": 2, "links": 3},
        "entity": {"links": 0},
        "decision": {"links": 0},
        "open_loop": {"links": 0},
    }.get(source_type, {"links": 0})
    return sorted(edges, key=lambda edge: (priority.get(edge.relation, 99), edge.target_id))


def _is_allowed_graph_edge(source_type: str, target_type: str, relation: str) -> bool:
    relation = relation or "links"
    if source_type == "artifact" and target_type in {"evidence", "claim"} and relation in {"linked_evidence", "linked_claims", "artifact_provenance", "links"}:
        return True
    if source_type == "evidence" and target_type == "claim" and relation in {"linked_claims", "links"}:
        return True
    if source_type == "evidence" and target_type == "artifact" and relation in {"artifact_provenance", "links"}:
        return True
    if source_type == "claim" and target_type == "evidence" and relation in {"supporting_evidence", "contradicting_evidence", "links"}:
        return True
    if source_type == "claim" and target_type == "artifact" and relation in {"artifact_provenance", "links"}:
        return True
    if source_type == "claim" and target_type in {"claim", "contradiction_log"} and relation in {"contradicting_evidence", "links"}:
        return True
    if source_type == "claim" and target_type == "pattern" and relation in {"linked_patterns", "links"}:
        return True
    if source_type == "pattern" and relation in {"supporting_records", "counterexamples", "links"}:
        return True
    if source_type == "episode" and target_type in {"entity", "evidence", "claim"} and relation in {"entities", "evidence", "claims", "links"}:
        return True
    if source_type == "entity" and target_type in {"episode", "claim", "open_loop"} and relation == "links":
        return True
    if source_type == "decision" and target_type == "open_loop" and relation == "links":
        return True
    if source_type == "open_loop" and target_type == "decision" and relation == "links":
        return True
    return False


def _target_type(rows_by_id: dict[str, sqlite3.Row], target_id: str) -> str:
    row = rows_by_id.get(target_id)
    return str(row["type"]) if row is not None else ""


def _graph_expansion_reason(relation: str, source_row: sqlite3.Row, target_row: sqlite3.Row) -> str:
    source_type = str(source_row["type"])
    target_type = str(target_row["type"])
    relation_label = relation or "links"
    return f"{source_type}.{relation_label}->{target_type}"


def _graph_score_reason(relation: str, source_row: sqlite3.Row, target_row: sqlite3.Row) -> str:
    relation_label = relation or "links"
    return f"graph:{source_row['type']}.{relation_label}->{target_row['type']}"


def _graph_relation_bonus(relation: str) -> float:
    return {
        "linked_evidence": 0.4,
        "linked_claims": 0.45,
        "artifact_provenance": 0.25,
        "linked_episodes": 0.3,
        "supporting_evidence": 0.5,
        "contradicting_evidence": 0.35,
        "linked_patterns": 0.4,
        "supporting_records": 0.35,
        "counterexamples": 0.3,
        "entities": 0.25,
        "evidence": 0.25,
        "claims": 0.25,
        "links": 0.2,
    }.get(relation or "links", 0.15)


def _render_expansion_path(path_ids: list[str], rows_by_id: dict[str, sqlite3.Row]) -> str:
    parts: list[str] = []
    for record_id in path_ids:
        row = rows_by_id.get(record_id)
        if row is None:
            parts.append(record_id)
            continue
        parts.append(f"{record_id}:{row['type']}")
    return " -> ".join(parts)


def _cross_domain_expansion_allowed(
    query_domains: set[str],
    source_id: str,
    target_id: str,
    path_ids: list[str],
    rows_by_id: dict[str, sqlite3.Row],
    coupled_pairs: set[tuple[str, str]],
) -> tuple[bool, str]:
    source_row = rows_by_id.get(source_id)
    target_row = rows_by_id.get(target_id)
    if source_row is None or target_row is None:
        return False, "missing_row"
    source_domain = str(source_row["domain_primary"] or source_row["arena"] or "")
    target_domain = str(target_row["domain_primary"] or target_row["arena"] or "")
    if not source_domain or not target_domain or source_domain == target_domain or source_domain == "cross_arena" or target_domain == "cross_arena":
        return True, "same_domain"
    if len(query_domains) > 1:
        return True, "query_explicitly_mentions_multiple_arenas"
    if _path_contains_bridge_pattern(path_ids, source_domain, target_domain, rows_by_id):
        return True, "linked_pattern_spans_multiple_arenas"
    if (source_domain, target_domain) in coupled_pairs or (target_domain, source_domain) in coupled_pairs:
        return True, "dreamer_summary_marks_domains_as_coupled"
    return False, "cross_domain_bridging_not_justified"


def _path_contains_bridge_pattern(path_ids: list[str], source_domain: str, target_domain: str, rows_by_id: dict[str, sqlite3.Row]) -> bool:
    for record_id in path_ids:
        row = rows_by_id.get(record_id)
        if row is None or str(row["type"]) != "pattern":
            continue
        areas = {str(row["domain_primary"] or row["arena"] or "")}
        areas.update(_json_list(row["domain_secondary"]))
        if source_domain in areas and target_domain in areas and len({domain for domain in areas if domain}) > 1:
            return True
    return False


def _dreamer_coupled_pairs(vault: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    reports_root = vault / "reports"
    if not reports_root.exists():
        return pairs
    for path in sorted(reports_root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        text = f"{json.dumps(doc.frontmatter, ensure_ascii=True)}\n{doc.body}".lower()
        if "coupled" not in text and "coupling" not in text:
            continue
        domain_tokens = [domain for domain in DOMAIN_KEYWORDS if domain in text]
        for i, left in enumerate(domain_tokens):
            for right in domain_tokens[i + 1:]:
                pairs.add((left, right))
    return pairs


def _explicit_query_domains(query: str) -> set[str]:
    lowered = query.lower()
    return {domain for domain, keywords in DOMAIN_KEYWORDS.items() if any(keyword in lowered for keyword in keywords)}



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
        # WO-ADJUTANT operational records: findable, but never crowding out
        # substantive memory in an assembled context.
        "confirmation": 0.6,
        "schedule": 0.4,
    }
    return boosts.get(record_type, 0.0)


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


def _calibration_line(fm: dict[str, Any]) -> str | None:
    """The source's predictive record, said plainly wherever it is retrieved
    (WO-PSYCHE Ship 2 §3.3: calibration is the honest form of 'the system
    knows things the owner doesn't')."""
    cal = fm.get("prediction_calibration")
    if not isinstance(cal, dict):
        return None
    return (
        f"- prediction record: {cal.get('hits', 0)} hit / {cal.get('misses', 0)} miss / "
        f"{cal.get('unclear', 0)} unclear / {cal.get('pending', 0)} pending — {cal.get('standing', 'unproven')}"
    )


def _claim_is_agent_operational(claim_text: str) -> bool:
    from .record_factory import _is_agent_operational

    try:
        return _is_agent_operational(claim_text)
    except Exception:
        return False


def _format_item_detail(item: RetrievalItem, path: Path, lean: bool = False) -> str:
    """Render one retrieved record for the model. ``lean`` omits scoring
    diagnostics and default-value noise (reason lines, 'none' evidence,
    status: active, chunk provenance) AT GENERATION — the conversation layer
    used to strip these with regexes after assembly, which silently coupled
    the savings to the exact rendered format. The noise a caller does not
    want is noise this function must not produce."""
    fallback = f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}`"
    if not lean:
        fallback += f" | {item.reason}"
    if not path.exists():
        return fallback
    try:
        doc = load_markdown(path)
    except Exception:
        return fallback
    fm = doc.frontmatter

    def _render(lines: list[str | None]) -> str:
        body = [ln for ln in lines if ln]
        expansion = _expansion_detail_lines(item).rstrip("\n")
        if expansion:
            body.append(expansion)
        if not lean:
            body.append(f"- reason: {item.reason}")
        return "\n".join(body)

    def _list_line(label: str, value: Any) -> str | None:
        joined = ", ".join(_json_list(value))
        if not joined and lean:
            return None
        return f"- {label}: {joined or 'none'}"

    if item.type == "evidence":
        return _render([
            f"### `{item.id}`",
            f"- summary: {fm.get('summary', item.summary)}",
            f"- source_type: {fm.get('source_type', 'unknown')}",
            _list_line("actors", fm.get("actors")),
            f"- reliability: {fm.get('reliability', 'unknown')}",
            _list_line("observed_facts", fm.get("observed_facts")),
            f"- link: `{item.path}`",
        ])
    if item.type == "artifact":
        return _render([
            f"### `{item.id}`",
            f"- file_name: {fm.get('file_name', 'unknown')}",
            f"- source_path: {fm.get('source_path', 'unknown')}",
            f"- source_type: {fm.get('source_type', 'unknown')}",
            f"- artifact_hash: {fm.get('artifact_hash', 'unknown')}",
            f"- file_ext: {fm.get('file_ext', 'unknown')}",
            f"- mime_type: {fm.get('mime_type', 'unknown')}",
            f"- size_bytes: {fm.get('size_bytes', 'unknown')}",
            f"- modified_at: {fm.get('modified_at', 'unknown')}",
            f"- imported_at: {fm.get('imported_at', 'unknown')}",
            f"- ingestion_status: {fm.get('ingestion_status', 'unknown')}",
            f"- sensitivity: {fm.get('sensitivity', 'unknown')}",
            f"- extracted_text_ref: {fm.get('extracted_text_ref', 'none')}",
            _list_line("linked_evidence", fm.get("linked_evidence")),
            _list_line("linked_claims", fm.get("linked_claims")),
            _list_line("parse_errors", fm.get("parse_errors")),
            f"- link: `{item.path}`",
        ])
    if item.type == "claim":
        status = fm.get("status", "unknown")
        # WO-GROUND Seam B: a retrieved self-report must never read as an
        # observation of the present. Generated here, at the rendering layer
        # (never regexed in afterwards — the lean-retrieval lesson). The
        # owner+subject fallback also banners legacy records that predate
        # the self_report class.
        banner = None
        claim_text = str(fm.get("claim_text", item.summary) or "")
        if str(fm.get("claim_class") or "") == "self_report" or (
            str(fm.get("owner") or "") == "agent" and _claim_is_agent_operational(claim_text)
        ):
            stamp = str(fm.get("created") or fm.get("created_at") or "an earlier date")
            banner = (
                f"- [agent self-report from {stamp} — for current state, self_state]"
            )
        return _render([
            f"### `{item.id}`",
            banner,
            f"- claim_text: {fm.get('claim_text', item.summary)}",
            f"- class: {fm.get('claim_class', 'unknown')}",
            f"- owner: {fm.get('owner', 'unknown')}",
            None if (lean and status == "active") else f"- status: {status}",
            f"- confidence: {fm.get('confidence', 'unknown')}",
            _list_line("supporting_evidence", fm.get("supporting_evidence")),
            _list_line("contradicting_evidence", fm.get("contradicting_evidence")),
            f"- link: `{item.path}`",
        ])
    if item.type == "pattern":
        return _render([
            f"### `{item.id}`",
            f"- hypothesis: {fm.get('hypothesis', item.summary)}",
            f"- pattern_type: {fm.get('pattern_type', 'unknown')}",
            f"- confidence: {fm.get('confidence', 'unknown')}",
            _calibration_line(fm),
            _list_line("supporting_records", fm.get("supporting_records")),
            _list_line("counterexamples", fm.get("counterexamples")),
            _list_line("alternative_explanations", fm.get("alternative_explanations")),
            _list_line("predictions", fm.get("predictions")),
            _list_line("evidence_needed", fm.get("evidence_needed")),
            f"- link: `{item.path}`",
        ])
    if item.type == "prediction":
        status = fm.get("status", "pending")
        verdict = str(fm.get("verdict") or "")
        return _render([
            f"### `{item.id}`",
            f"- expectation: {fm.get('expectation', item.summary)}",
            f"- source: {fm.get('source_id', 'unknown')}",
            f"- status: {status}" + (f" — verdict: {verdict} ({fm.get('scored_at', '')})" if verdict else f" — review after {fm.get('review_after', '?')}"),
            (f"- verdict_note: {fm.get('verdict_note')}" if fm.get("verdict_note") else None),
            _list_line("verdict_evidence", fm.get("verdict_evidence")) if verdict in {"hit", "miss"} else None,
            f"- link: `{item.path}`",
        ])
    if item.type == "skeptical_review":
        return _render([
            f"### `{item.id}`",
            f"- summary: {fm.get('summary', item.summary)}",
            f"- reviewed_record_id: {fm.get('reviewed_record_id', 'unknown')}",
            f"- risk: {fm.get('risk', 'unknown')}",
            f"- recommended_action: {fm.get('recommended_action', 'unknown')}",
            _list_line("reasoning_errors", fm.get("reasoning_errors")),
            f"- link: `{item.path}`",
        ])
    if item.type == "report":
        return _render([
            f"### `{item.id}`",
            f"- summary: {fm.get('summary', item.summary)}",
            f"- task: {fm.get('task', 'unknown')}",
            f"- link: `{item.path}`",
        ])
    if item.type == "knowledge":
        # Chunk provenance beyond source_document restates the summary
        # breadcrumb and the link; source_document itself stays — the
        # citation rule reads it.
        return _render([
            f"### `{item.id}`",
            f"- summary: {fm.get('summary', item.summary)}",
            _calibration_line(fm),
            f"- source_document: {fm.get('source_document', 'unknown')}",
            None if lean else f"- source_section: {fm.get('source_section', 'unknown')}",
            None if lean else f"- source_ref: {fm.get('source_ref', 'unknown')}",
            None if lean else f"- chunk_index: {fm.get('chunk_index', 'unknown')}",
            None if lean else f"- total_chunks: {fm.get('total_chunks', 'unknown')}",
            f"- link: `{item.path}`",
        ])
    if item.type == "episode":
        return _render([
            f"### `{item.id}`",
            f"- summary: {fm.get('summary', item.summary)}",
            f"- link: `{item.path}`",
        ])
    return fallback


def _expansion_detail_lines(item: RetrievalItem) -> str:
    if not item.expanded:
        return ""
    lines = [
        f"- expansion_source: `{item.expansion_source}`",
        f"- expansion_path: {item.expansion_path}",
        f"- expansion_reason: {item.expansion_reason}",
        f"- hop: {item.hop}",
    ]
    return "\n".join(lines) + "\n"


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
    direct_loaded: list[RetrievalItem],
    graph_loaded: list[RetrievalItem],
    rejected: list[RetrievalItem],
    graph_blocked: list[RetrievalItem],
    *,
    retrieval_mode: str = "legacy",
    fusion_enabled: bool = False,
    sql_candidate_count: int | None = None,
    fts_candidate_count: int | None = None,
    vector_candidate_count: int | None = None,
    fused_candidate_count: int | None = None,
    overlap_count: int | None = None,
    rrf_k: int | None = None,
    per_layer_limit: int | None = None,
    fused_limit: int | None = None,
    fts_mode: str | None = None,
    embedding_mode: str | None = None,
) -> None:
    try:
        _ensure_retrieval_log_columns(conn)
        conn.execute(
            """
            INSERT INTO retrieval_log (
                conversation_id, user_query, domain_context, classification_confidence,
                files_loaded, direct_files_loaded, graph_files_loaded, files_rejected, rejection_reasons,
                graph_blocked_count, graph_blocked_reasons, token_count, privacy_level,
                cross_compartment, model_used, retrieval_mode, fusion_enabled,
                sql_candidate_count, fts_candidate_count, vector_candidate_count, fused_candidate_count,
                overlap_count, rrf_k, per_layer_limit, fused_limit, fts_mode, embedding_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                query,
                arena,
                confidence,
                json.dumps([item.id for item in loaded]),
                json.dumps([item.id for item in direct_loaded]),
                json.dumps([item.id for item in graph_loaded]),
                json.dumps([item.id for item in rejected]),
                json.dumps([item.reason for item in rejected]),
                len(graph_blocked),
                json.dumps([item.reason for item in graph_blocked]),
                sum(approx_word_count(item.summary) for item in loaded),
                "mixed" if any(item.reason == "quarantined" for item in rejected) or graph_blocked else "normal",
                int(bool(rejected or graph_blocked)),
                None,
                retrieval_mode,
                int(bool(fusion_enabled)),
                sql_candidate_count,
                fts_candidate_count,
                vector_candidate_count,
                fused_candidate_count,
                overlap_count,
                rrf_k,
                per_layer_limit,
                fused_limit,
                fts_mode,
                embedding_mode,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def _ensure_retrieval_log_columns(conn: sqlite3.Connection) -> None:
    try:
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(retrieval_log)").fetchall()}
    except sqlite3.Error:
        return
    desired = {
        "retrieval_mode": "TEXT",
        "fusion_enabled": "BOOLEAN",
        "sql_candidate_count": "INTEGER",
        "fts_candidate_count": "INTEGER",
        "vector_candidate_count": "INTEGER",
        "fused_candidate_count": "INTEGER",
        "overlap_count": "INTEGER",
        "rrf_k": "INTEGER",
        "per_layer_limit": "INTEGER",
        "fused_limit": "INTEGER",
        "fts_mode": "TEXT",
        "embedding_mode": "TEXT",
    }
    for column, column_type in desired.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE retrieval_log ADD COLUMN {column} {column_type}")
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


# ── Cross-conversation preamble ──────────────────────────────────────────────


def _recent_conversation_turns(vault: Path, conversation_id: str, limit: int = 4) -> list[dict[str, str]]:
    """Return the last *limit* turns for *conversation_id* from today's transcript."""
    today_transcript = vault / "transcripts" / f"{today_iso()}.md"
    if not today_transcript.exists():
        return []
    try:
        text = today_transcript.read_text(encoding="utf-8")
    except Exception:
        return []
    turns: list[dict[str, str]] = []
    target_header = f"[{conversation_id}]"
    in_block = False
    for line in text.splitlines():
        if line.startswith("## Conversation — "):
            in_block = target_header in line
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped.startswith("USER:") or stripped.startswith("LISAN:"):
            speaker, _, utterance = stripped.partition(": ")
            if utterance.strip():
                turns.append({"speaker": speaker, "text": utterance.strip()})
    return turns[-limit:]


def _is_fresh_conversation(vault: Path, conversation_id: str) -> bool:
    """True when this conversation has no narrative state and no prior USER turn today."""
    narrative_path = vault / "transcripts" / "narrative" / f"{conversation_id}.json"
    if narrative_path.exists():
        return False
    today_transcript = vault / "transcripts" / f"{today_iso()}.md"
    if not today_transcript.exists():
        return True
    try:
        text = today_transcript.read_text(encoding="utf-8")
    except Exception:
        return True
    marker = f"[{conversation_id}]"
    if marker not in text:
        return True
    # The current turn writes its USER line BEFORE the assembler runs. One
    # USER line means this is still the opening turn; two or more means the
    # conversation has history.
    user_count = 0
    for block in text.split("## Conversation — "):
        if marker not in block:
            continue
        for line in block.splitlines():
            if line.strip().startswith("USER:"):
                user_count += 1
    return user_count <= 1


def _recent_activity_block(vault: Path) -> str:
    """Summarize today\u2019s state updates and fresh open loops across all areas."""
    today = today_iso()
    state_lines: list[str] = []
    state_dir = vault / "state"
    if state_dir.exists():
        for path in sorted(state_dir.glob("*-current.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            updated = str(doc.frontmatter.get("updated") or "").strip()
            if updated != today:
                continue
            domain = str(doc.frontmatter.get("domain_primary") or path.stem.replace("-current", ""))
            summary = str(doc.frontmatter.get("summary") or "").strip()
            if summary:
                state_lines.append(f"- {domain}: {summary}")
    loop_lines: list[str] = []
    loop_dir = vault / "open_loops"
    if loop_dir.exists():
        for path in sorted(loop_dir.glob(f"{today}-*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            title = str(doc.frontmatter.get("summary") or path.stem)
            next_action = str(doc.frontmatter.get("next_action") or "").strip()
            domain = str(doc.frontmatter.get("domain_primary") or "")
            suffix = f" \u2192 {next_action}" if next_action else ""
            domain_tag = f" [{domain}]" if domain else ""
            loop_lines.append(f"- {title}{domain_tag}{suffix}")
    if not state_lines and not loop_lines:
        return ""
    lines = ["## Recent Activity (today, across all conversations)"]
    if state_lines:
        lines.append("")
        lines.append("### State updated today")
        lines.extend(state_lines)
    if loop_lines:
        lines.append("")
        lines.append("### Open loops opened today")
        lines.extend(loop_lines)
    return "\n".join(lines)
