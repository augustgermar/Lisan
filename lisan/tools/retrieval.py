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
from ..tools.common import iter_markdown_files
from ..tools.tracing import record_retrieval_result
from ..tools.vector_store import VectorScorer, build_query_scorer
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


def assemble_context(
    query: str,
    domain: str | None = None,
    arena: str | None = None,
    vault: Path | None = None,
    db_path: Path | None = None,
    conversation_id: str | None = None,
    include_quarantined: bool = False,
) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    result = retrieve_context(query=query, domain=domain, arena=arena, vault=vault, db_path=db_path, conversation_id=conversation_id, include_quarantined=include_quarantined)
    sections: list[str] = ["# Assembled Context", ""]
    sections.append("## Assistant Identity")
    sections.append(
        "You are Lisan, the user's local personal assistant and memory system. "
        "Never answer as a retrieved person or entity. Retrieved records describe the user's world; they do not define your identity."
    )
    sections.append("")
    sections.append(f"domain: {result.domain}")
    sections.append(f"domain_confidence: {result.confidence:.2f}")
    sections.append(f"direct_matches: {len(result.direct_loaded)}")
    sections.append(f"graph_expanded_matches: {len(result.expanded_loaded)}")
    sections.append("")
    sections.append(f"query: {query}")
    sections.append("")

    # Inject the last few turns of the current conversation so the writer is
    # anchored to the active thread. Without this, short or dense turns drift
    # toward unrelated earlier context because their query signal is too weak.
    if conversation_id:
        recent = _recent_conversation_turns(vault, conversation_id, limit=4)
        if recent:
            sections.append("## Current Conversation Thread")
            sections.append("Most recent turns (oldest first). Weight these heavily — they define the active topic.")
            sections.append("")
            for turn in recent:
                sections.append(f"{turn['speaker']}: {turn['text']}")
            sections.append("")

    for rel in ["primer/identity.md", "primer/operating-style.md", "primer/current-brief.md"]:
        path = vault / rel
        if path.exists():
            sections.append(f"## {rel}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    # v0.1.7: cross-conversation "Recent Activity" preamble.
    # When a conversation is freshly opened (no narrative state file AND no
    # USER turns for that conversation_id in today's transcript), inject a
    # compact summary of today's state updates and open loops across all
    # domains. Lets a new conversation react to cumulative load from earlier
    # conversations the same day. Lifted out of elicitor_session so the
    # extraction path also gets it.
    if conversation_id and _is_fresh_conversation(vault, conversation_id):
        preamble = _recent_activity_block(vault)
        if preamble:
            sections.append(preamble)
            sections.append("")

    unique_loaded = _unique_items(result.loaded)
    groups = {
        "artifact": [item for item in unique_loaded if item.type == "artifact"],
        "evidence": [item for item in unique_loaded if item.type == "evidence"],
        "claim": [item for item in unique_loaded if item.type == "claim"],
        "pattern": [item for item in unique_loaded if item.type == "pattern"],
        "skeptical_review": [item for item in unique_loaded if item.type == "skeptical_review"],
        "episode": [item for item in unique_loaded if item.type == "episode"],
        "report": [item for item in unique_loaded if item.type == "report"],
        "state": [item for item in unique_loaded if item.type == "state"],
        "other": [item for item in unique_loaded if item.type not in {"artifact", "evidence", "claim", "pattern", "skeptical_review", "episode", "report", "state"}],
    }

    section_order = [
        ("## Artifacts", groups["artifact"]),
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
        sections.append("## Rejected By Quarantine")
        for item in result.rejected:
            sections.append(f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}")
        sections.append("")

    if result.graph_blocked:
        sections.append("## Graph Blocked Expansions")
        for item in result.graph_blocked:
            sections.append(
                f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason} | "
                f"{item.expansion_reason} | {item.expansion_path}"
            )
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
    include_quarantined: bool = False,
) -> RetrievalResult:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    config = load_config()
    retrieval_settings = _retrieval_fusion_settings(config)
    # Build two query variants:
    # - domain_query: always blends recent turns so domain inference stays
    #   anchored to the active thread even on short or acknowledgment turns.
    # - effective_query: blends recent turns for FTS/SQL/vector only when the
    #   current turn is short (< 5 words); longer turns have enough signal of
    #   their own and blending adds noise.
    query_word_count = len(query.split())
    if conversation_id:
        recent = _recent_conversation_turns(vault, conversation_id, limit=3)
        if recent:
            recent_text = " ".join(t["text"] for t in recent)
            domain_query = f"{query} {recent_text}"
            effective_query = domain_query if query_word_count < 5 else query
        else:
            domain_query = query
            effective_query = query
    else:
        domain_query = query
        effective_query = query
    inferred_domain, confidence = _infer_domain(domain_query, domain or arena)
    active_contexts = _active_contexts(inferred_domain, domain_query)

    # Embed the query exactly once and load embeddings.bin exactly once per
    # retrieval call. The scorer ranks every candidate against this preloaded
    # map — no per-candidate disk reads, no per-candidate query embeds. The
    # index lives next to the SQLite file so tests and external vaults resolve
    # the right one.
    vector_scorer = build_query_scorer(
        effective_query,
        embeddings_file=db_path.parent / "embeddings.bin",
        config=config,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        file_rows = conn.execute("SELECT * FROM files").fetchall()
        link_rows = conn.execute("SELECT source_id, target_id, relationship_type FROM links").fetchall()
        quarantined_artifact_ids, quarantined_batch_ids = _quarantine_sets(conn)
        rows_by_id = {str(row["id"]): row for row in file_rows}
        rejected = _collect_rejected_items(
            file_rows,
            active_contexts=active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
        )

        if retrieval_settings["enabled"] and retrieval_settings["method"] == "rrf":
            sql_candidates = _sql_ranked_candidates(
                file_rows,
                inferred_domain,
                query=effective_query,
                active_contexts=active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
                limit=retrieval_settings["per_layer_limit"],
                recency_decay_days=retrieval_settings["recency_decay_days"],
            )
            fts_candidates, fts_mode = _fts_ranked_candidates(
                conn,
                file_rows=file_rows,
                query=effective_query,
                active_contexts=active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
                limit=retrieval_settings["per_layer_limit"],
            )
            vector_candidates = _vector_ranked_candidates(
                file_rows,
                vector_scorer=vector_scorer,
                active_contexts=active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
                limit=retrieval_settings["per_layer_limit"],
            )
            direct_loaded, fusion_stats = _fuse_ranked_candidates(
                rows_by_id=rows_by_id,
                sql_candidates=sql_candidates,
                fts_candidates=fts_candidates,
                vector_candidates=vector_candidates,
                rrf_k=retrieval_settings["rrf_k"],
                fused_limit=retrieval_settings["fused_limit"],
            )
            direct_loaded = _demote_graph_neighbors(
                direct_loaded,
                rows_by_id=rows_by_id,
                link_rows=link_rows,
                source_order=fusion_stats["source_order"],
            )
            direct_loaded = [
                item
                for item in direct_loaded
                if _visibility_block_reason(
                    rows_by_id[item.id],
                    active_contexts,
                    quarantined_artifact_ids=quarantined_artifact_ids,
                    quarantined_batch_ids=quarantined_batch_ids,
                    include_quarantined=include_quarantined,
                ) is None
            ]
            fusion_stats["fused_candidate_count"] = len(direct_loaded)
            graph_loaded, graph_blocked = _expand_graph(
                direct_loaded,
                rows_by_id=rows_by_id,
                link_rows=link_rows,
                query=effective_query,
                inferred_domain=inferred_domain,
                vault=vault,
                active_contexts=active_contexts,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
                max_hops=2,
                max_expanded_records=5,
                max_cross_domain_records=2,
            )
            combined_loaded = direct_loaded + graph_loaded
            record_retrieval_result(len(direct_loaded), len(graph_loaded))
            _log_retrieval(
                conn,
                conversation_id=conversation_id,
                query=query,
                arena=inferred_domain,
                confidence=confidence,
                loaded=combined_loaded,
                direct_loaded=direct_loaded,
                graph_loaded=graph_loaded,
                rejected=rejected,
                graph_blocked=graph_blocked,
                retrieval_mode="rrf",
                fusion_enabled=True,
                embedding_mode=vector_scorer.mode_used,
                sql_candidate_count=fusion_stats["sql_candidate_count"],
                fts_candidate_count=fusion_stats["fts_candidate_count"],
                vector_candidate_count=fusion_stats["vector_candidate_count"],
                fused_candidate_count=fusion_stats["fused_candidate_count"],
                overlap_count=fusion_stats["overlap_count"],
                rrf_k=retrieval_settings["rrf_k"],
                per_layer_limit=retrieval_settings["per_layer_limit"],
                fused_limit=retrieval_settings["fused_limit"],
                fts_mode=fts_mode,
            )
            return RetrievalResult(
                domain=inferred_domain,
                confidence=confidence,
                loaded=combined_loaded,
                direct_loaded=direct_loaded,
                expanded_loaded=graph_loaded,
                rejected=rejected,
                graph_blocked=graph_blocked,
                prompt=query,
            )

        fts_ids = _fts_candidate_ids(conn, effective_query)
        all_items = [
            _score_row(
                row,
                effective_query,
                inferred_domain,
                vault,
                active_contexts,
                fts_ids,
                vector_scorer=vector_scorer,
                quarantined_artifact_ids=quarantined_artifact_ids,
                quarantined_batch_ids=quarantined_batch_ids,
                include_quarantined=include_quarantined,
            )
            for row in file_rows
        ]
        visible_items = [item for item in all_items if item is not None and not _is_blocked_visibility_reason(item.reason)]
        visible_items.sort(key=lambda item: (_type_boost(item.type), item.score, item.summary), reverse=True)
        direct_loaded = visible_items[:15]
        graph_loaded, graph_blocked = _expand_graph(
            direct_loaded,
            rows_by_id=rows_by_id,
            link_rows=link_rows,
            query=query,
            inferred_domain=inferred_domain,
            vault=vault,
            active_contexts=active_contexts,
            quarantined_artifact_ids=quarantined_artifact_ids,
            quarantined_batch_ids=quarantined_batch_ids,
            include_quarantined=include_quarantined,
            max_hops=2,
            max_expanded_records=5,
            max_cross_domain_records=2,
        )
        combined_loaded = direct_loaded + graph_loaded
        record_retrieval_result(len(direct_loaded), len(graph_loaded))
        _log_retrieval(
            conn,
            conversation_id=conversation_id,
            query=query,
            arena=inferred_domain,
            confidence=confidence,
            loaded=combined_loaded,
            direct_loaded=direct_loaded,
            graph_loaded=graph_loaded,
            rejected=rejected,
            graph_blocked=graph_blocked,
            retrieval_mode="legacy",
            fusion_enabled=False,
            embedding_mode=vector_scorer.mode_used,
            sql_candidate_count=0,
            fts_candidate_count=0,
            vector_candidate_count=0,
            fused_candidate_count=len(direct_loaded),
            overlap_count=0,
            rrf_k=None,
            per_layer_limit=None,
            fused_limit=None,
            fts_mode="match_fallback",
        )
        return RetrievalResult(
            domain=inferred_domain,
            confidence=confidence,
            loaded=combined_loaded,
            direct_loaded=direct_loaded,
            expanded_loaded=graph_loaded,
            rejected=rejected,
            graph_blocked=graph_blocked,
            prompt=query,
        )
    finally:
        conn.close()


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
) -> tuple[list[RetrievalItem], dict[str, Any]]:
    candidate_lists = [sql_candidates, fts_candidates, vector_candidates]
    rrf_scores: dict[str, float] = defaultdict(float)
    source_order: dict[str, list[str]] = defaultdict(list)
    for candidates in candidate_lists:
        for rank, candidate in enumerate(candidates, start=1):
            rrf_scores[candidate.id] += 1.0 / (rrf_k + rank)
            if candidate.source not in source_order[candidate.id]:
                source_order[candidate.id].append(candidate.source)

    fused_items: list[RetrievalItem] = []
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
        sources = source_order.get(record_id, [])
        fused_items.append(
            _item_from_row(
                row,
                score=round(score, 6),
                reason=f"rrf:{'+'.join(sources)}" if sources else "rrf",
            )
        )
        if len(fused_items) >= fused_limit:
            break

    stats = {
        "sql_candidate_count": len(sql_candidates),
        "fts_candidate_count": len(fts_candidates),
        "vector_candidate_count": len(vector_candidates),
        "fused_candidate_count": len(fused_items),
        "overlap_count": sum(1 for sources in source_order.values() if len(set(sources)) > 1),
        "source_order": source_order,
    }
    return fused_items, stats


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
        domains = {str(row["domain_primary"] or row["arena"] or "")}
        domains.update(_json_list(row["domain_secondary"]))
        if source_domain in domains and target_domain in domains and len({domain for domain in domains if domain}) > 1:
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
            f"{_expansion_detail_lines(item)}"
            f"- reason: {item.reason}"
        )
    if item.type == "artifact":
        return (
            f"### `{item.id}`\n"
            f"- file_name: {fm.get('file_name', 'unknown')}\n"
            f"- source_path: {fm.get('source_path', 'unknown')}\n"
            f"- source_type: {fm.get('source_type', 'unknown')}\n"
            f"- artifact_hash: {fm.get('artifact_hash', 'unknown')}\n"
            f"- file_ext: {fm.get('file_ext', 'unknown')}\n"
            f"- mime_type: {fm.get('mime_type', 'unknown')}\n"
            f"- size_bytes: {fm.get('size_bytes', 'unknown')}\n"
            f"- modified_at: {fm.get('modified_at', 'unknown')}\n"
            f"- imported_at: {fm.get('imported_at', 'unknown')}\n"
            f"- ingestion_status: {fm.get('ingestion_status', 'unknown')}\n"
            f"- sensitivity: {fm.get('sensitivity', 'unknown')}\n"
            f"- extracted_text_ref: {fm.get('extracted_text_ref', 'none')}\n"
            f"- linked_evidence: {', '.join(_json_list(fm.get('linked_evidence'))) or 'none'}\n"
            f"- linked_claims: {', '.join(_json_list(fm.get('linked_claims'))) or 'none'}\n"
            f"- parse_errors: {', '.join(_json_list(fm.get('parse_errors'))) or 'none'}\n"
            f"- link: `{item.path}`\n"
            f"{_expansion_detail_lines(item)}"
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
            f"{_expansion_detail_lines(item)}"
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
            f"{_expansion_detail_lines(item)}"
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
            f"{_expansion_detail_lines(item)}"
            f"- reason: {item.reason}"
        )
    if item.type == "episode":
        return f"### `{item.id}`\n- summary: {fm.get('summary', item.summary)}\n- link: `{item.path}`\n{_expansion_detail_lines(item)}- reason: {item.reason}"
    return f"- `{item.id}` | {item.type} | {item.summary} | `{item.path}` | {item.reason}"


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


# ── Cross-conversation preamble (v0.1.7) ─────────────────────────────────────


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
    """Summarize today\u2019s state updates and fresh open loops across all domains."""
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
