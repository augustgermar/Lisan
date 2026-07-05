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
from .learned_edges import learned_edges_settings, learned_partners
from .retrieval_layers import RetrievalItem, RetrievalResult, _LayerCandidate
from .retrieval_layers import (
    _retrieval_fusion_settings,
    _collect_rejected_items,
    _sql_ranked_candidates,
    _fts_ranked_candidates,
    _vector_ranked_candidates,
    _fuse_ranked_candidates,
    _demote_graph_neighbors,
    _item_from_row,
    _truncate_layer_candidates,
    _row_by_id,
    _term_count_score,
    _sql_metadata_score,
    _infer_domain,
    _active_contexts,
    _score_row,
    _unique_items,
    _is_blocked_visibility_reason,
    _quarantine_sets,
    _visibility_block_reason,
)
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
    _type_boost,
    _metadata_haystack,
    _format_item_detail,
    _expansion_detail_lines,
    _fts_candidate_ids,
    _fts_escape,
    _json_list,
    _log_retrieval,
    _ensure_retrieval_log_columns,
    _load_relevant_contradictions,
    _recent_conversation_turns,
    _is_fresh_conversation,
    _recent_activity_block,
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








def _record_date(path: Path) -> str:
    try:
        from ..frontmatter import load_markdown

        fm = load_markdown(path).frontmatter
        return str(fm.get("updated") or fm.get("created") or "")
    except Exception:
        return ""


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
        f'IDENTITY NOTE: "I/me" = {assistant_display_name(vault)}, the assistant. '
        f'"You/your" = {principal_name(vault)}, the principal. '
        "The profile below describes the principal, not the assistant."
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
            if rel == "primer/identity.md":
                sections.append("PRINCIPAL_PROFILE (about the user, not about you):")
            else:
                sections.append(f"## {rel}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    # Cross-conversation "Recent Activity" preamble.
    # When a conversation is freshly opened (no narrative state file AND no
    # USER turns for that conversation_id in today's transcript), inject a
    # compact summary of today's state updates and open loops across all
    # areas. Lets a new conversation react to cumulative load from earlier
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
            # Every record carries its date: stored text may say "today" or
            # "tomorrow" frozen at write time, and the reader can only
            # resolve those against the record's own date.
            stamp = _record_date(path)
            if stamp:
                details = details.rstrip() + f"\n- record_date: {stamp}"
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
    reply_query = ""
    if conversation_id:
        recent = _recent_conversation_turns(vault, conversation_id, limit=3)
        if recent:
            recent_text = " ".join(t["text"] for t in recent)
            domain_query = f"{query} {recent_text}"
            effective_query = domain_query if query_word_count < 5 else query
        else:
            domain_query = query
            effective_query = query
        # Reply-query pass: the assistant's previous reply is its OWN retrieval
        # intent — the thread it is actively developing, which the user's next
        # message may reference without naming. It runs as separate queries,
        # never blended into the user's (two speakers averaged into one vector
        # match neither). Trivial acknowledgments carry no intent; skip them.
        for turn in reversed(recent or []):
            if turn.get("speaker") == "LISAN":
                candidate_reply = str(turn.get("text") or "").strip()
                if len(candidate_reply.split()) >= retrieval_settings["reply_query_min_words"]:
                    reply_query = candidate_reply[:600]
                break
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
        try:
            file_rows = conn.execute("SELECT * FROM files").fetchall()
            link_rows = conn.execute("SELECT source_id, target_id, relationship_type FROM links").fetchall()
        except sqlite3.OperationalError:
            # An uninitialized index (fresh install, first turn before any
            # rebuild) means "nothing stored yet", not a crash.
            return RetrievalResult(
                domain=domain or "",
                confidence=0.0,
                loaded=[],
                direct_loaded=[],
                expanded_loaded=[],
                rejected=[],
                graph_blocked=[],
                prompt=effective_query,
            )
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
            reply_lanes = []
            if reply_query and retrieval_settings["reply_query_enabled"]:
                reply_fts, _ = _fts_ranked_candidates(
                    conn,
                    file_rows=file_rows,
                    query=reply_query,
                    active_contexts=active_contexts,
                    quarantined_artifact_ids=quarantined_artifact_ids,
                    quarantined_batch_ids=quarantined_batch_ids,
                    include_quarantined=include_quarantined,
                    limit=retrieval_settings["reply_query_limit"],
                )
                reply_scorer = build_query_scorer(
                    reply_query,
                    embeddings_file=db_path.parent / "embeddings.bin",
                    config=config,
                )
                reply_vector = _vector_ranked_candidates(
                    file_rows,
                    vector_scorer=reply_scorer,
                    active_contexts=active_contexts,
                    quarantined_artifact_ids=quarantined_artifact_ids,
                    quarantined_batch_ids=quarantined_batch_ids,
                    include_quarantined=include_quarantined,
                    limit=retrieval_settings["reply_query_limit"],
                )
                for lane in (reply_fts, reply_vector):
                    for candidate in lane:
                        candidate.source = f"{candidate.source}_reply"
                reply_lanes = [reply_fts, reply_vector]
            # Learned-edge lane: behavioral associations mined from retrieval
            # co-selection history (learned_edges.py). Seeds are the top
            # user-lane hits; the lane only ever ADDS candidates.
            learned_lane: list[_LayerCandidate] = []
            edge_settings = learned_edges_settings(config)
            if edge_settings["enabled"]:
                seeds: list[str] = []
                for candidate in [*fts_candidates, *vector_candidates]:
                    if candidate.id not in seeds:
                        seeds.append(candidate.id)
                    if len(seeds) >= int(edge_settings["seed_count"]):
                        break
                for partner_id, npmi in learned_partners(
                    conn, seeds, limit=int(edge_settings["lane_limit"]), exclude=set(seeds)
                ):
                    row = rows_by_id.get(partner_id)
                    if row is None:
                        continue
                    if _visibility_block_reason(
                        row,
                        active_contexts,
                        quarantined_artifact_ids=quarantined_artifact_ids,
                        quarantined_batch_ids=quarantined_batch_ids,
                        include_quarantined=include_quarantined,
                    ) is not None:
                        continue
                    learned_lane.append(_LayerCandidate(id=partner_id, score=npmi, source="learned_edge"))
            direct_loaded, fusion_stats = _fuse_ranked_candidates(
                rows_by_id=rows_by_id,
                sql_candidates=sql_candidates,
                fts_candidates=fts_candidates,
                vector_candidates=vector_candidates,
                rrf_k=retrieval_settings["rrf_k"],
                fused_limit=retrieval_settings["fused_limit"],
                extra_candidate_lists=[*reply_lanes, learned_lane],
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

