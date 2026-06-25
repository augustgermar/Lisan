from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import AssemblerAgent, InterlocutorAgent, ListenerAgent, SkepticAgent, WriterAgent
from ..tools.heuristic_gate import is_correction_turn
from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .elicitor_session import run_elicitor_session
from .domain_fields import with_domain_fields
from .firewall import scan_text
from .log import log_error
from .epistemic import listify
from .narrative_state import load_narrative_state
from .retrieval import retrieve_context
from .deixis import has_unresolved_token, render_deixis, tokenize_principal, tokenize_principal_obj
from .record_fanout import (
    basis_or_default,
    fanout_claims,
    fanout_decisions,
    fanout_evidence,
    fanout_open_loops,
    fanout_state_updates,
    index_created_record,
)
from .rebuild_index import open_index_connection
from .tracing import record_inline_step
from .record_factory import (
    CreatedRecord,
    new_entity,
    supersede_record,
)
from .reference_resolution import normalize_text, resolve_reference, resolution_action
from .transcripts import append_transcript
from ..agents.writer import _truncate_summary as _truncate_summary_boundary



@dataclass(slots=True)
class MemoryPipelineResult:
    transcript_path: Path
    draft_path: Path | None
    listener: dict[str, Any]
    writer: dict[str, Any] | None
    skeptic: dict[str, Any] | None
    interlocutor: dict[str, Any] | None
    action: str
    mode: str
    elicitor: dict[str, Any] | None = None
    narrative_state_path: Path | None = None
    narrative_state: dict[str, Any] | None = None
    skeptic_approved: bool = True
    entities_touched: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class RoutingContext:
    text: str
    listener: dict[str, Any]
    prior_state: Any
    conversation_id: str | None
    vault: Path


@dataclass(slots=True)
class RoutingDecision:
    listener: dict[str, Any]
    action: str
    mode: str
    applied_overrides: tuple[str, ...] = ()


def run_memory_pipeline(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
    conversation_policy: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> MemoryPipelineResult:
    record_inline_step("memory_pipeline.start")
    transcript_path = append_transcript(vault=vault, conversation_id=conversation_id, speaker=speaker, text=text)
    record_inline_step("memory_pipeline.transcript")
    fw = scan_text(text, vault=vault)
    text = fw.text  # use sanitized version for all downstream agents
    prior_state = load_narrative_state(vault=vault, conversation_id=conversation_id)
    record_inline_step("memory_pipeline.listener")
    listener = ListenerAgent(vault=vault).run_json(text, provider=provider, model=model, provider_error_mode="raise")
    routing = route_turn(
        RoutingContext(
            text=text,
            listener=listener,
            prior_state=prior_state,
            conversation_id=conversation_id,
            vault=vault,
        )
    )
    listener = routing.listener
    action = routing.action
    mode = routing.mode

    if action == "skip":
        response_text = _build_skip_response(
            vault=vault,
            text=text,
            conversation_id=conversation_id,
            conversation_policy=conversation_policy,
            db_path=db_path,
        )
        return MemoryPipelineResult(
            transcript_path=transcript_path,
            draft_path=None,
            listener=listener,
            writer=None,
            skeptic=None,
            interlocutor={"response": response_text},
            action=action,
            mode=mode,
        )

    if mode == "elicitor":
        record_inline_step("memory_pipeline.elicitor")
        elicitor_result = run_elicitor_session(
            vault=vault,
            text=text,
            conversation_id=conversation_id,
            speaker=speaker,
            provider=provider,
            model=model,
            transcript_path=transcript_path,
            conversation_policy=conversation_policy,
            db_path=db_path,
        )
        return MemoryPipelineResult(
            transcript_path=transcript_path,
            draft_path=elicitor_result.draft_path,
            listener=listener,
            writer=None,
            skeptic=None,
            interlocutor=None,
            action=action,
            mode=mode,
            elicitor=elicitor_result.response,
            narrative_state_path=elicitor_result.state_path,
            narrative_state=elicitor_result.narrative_state,
        )

    record_inline_step("memory_pipeline.assembler")
    # Finding #12: pass conversation_id so the cross-conversation preamble
    # fires on the extraction path as well as the elicitor path.
    context = AssemblerAgent(vault=vault).run(text, conversation_id=conversation_id).text
    task = _choose_task(text=text, listener=listener)
    if str(listener.get("memory_type") or "").lower() == "correction":
        correction_ctx = _build_correction_context(text, db_path=db_path)
        if correction_ctx:
            context = context + "\n\n" + correction_ctx
    significance = "high" if action == "full" else "medium"
    common_kwargs = {
        "significance": significance,
        "provider": provider,
        "model": model,
        "provider_error_mode": "raise",
        "context": context,
        "transcript": str(transcript_path.relative_to(vault)),
        "conversation_policy": json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    }
    record_inline_step("memory_pipeline.writer")
    # v0.1.9: the episode path is split. The core call returns body + claims;
    # the artifact call returns entities / decisions / open loops / state /
    # evidence. Non-episode tasks stay single-shot — they're already small.
    if task == "episode":
        writer_core = WriterAgent(vault=vault).run_json(
            text, task="episode_core", **common_kwargs,
        )
        writer = dict(writer_core)
    else:
        writer = WriterAgent(vault=vault).run_json(
            text, task=task, **common_kwargs,
        )
        writer_core = writer
    writer_core = tokenize_principal_obj(writer_core, vault)
    writer = tokenize_principal_obj(writer, vault)
    record_inline_step("memory_pipeline.skeptic")
    skeptic = SkepticAgent(vault=vault).run_json(
        json.dumps(writer_core, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    skeptic_approved = _skeptic_approves(skeptic)
    record_inline_step("memory_pipeline.interlocutor")
    # Finding 1: do not forward skeptic flags to the interlocutor.
    # Skeptic uncertainty about a memory record was bleeding into the user-facing
    # response (e.g. "this family member" instead of the named person). The
    # interlocutor speaks to the user; it should not see internal review notes.
    interlocutor = InterlocutorAgent(vault=vault).run_json(
        json.dumps(
            _interlocutor_input(writer=writer_core, listener=listener, prior_state=prior_state, user_text=text, vault=vault),
            indent=2,
            ensure_ascii=True,
        ),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    # v0.1.9: only run the artifact pass when the skeptic approved the core.
    # If the core is rejected, we hold the draft as needs_revision and never
    # spend a second writer call on derived artifacts that would be skipped
    # in fanout anyway.
    writer_artifacts: dict[str, Any] = {}
    if task == "episode" and skeptic_approved:
        record_inline_step("memory_pipeline.writer.artifacts")
        writer_artifacts = WriterAgent(vault=vault).run_json(
            text,
            task="episode_artifacts",
            prior_writer_core=json.dumps(writer_core, indent=2, ensure_ascii=True),
            **common_kwargs,
        )
        writer = _merge_writer_outputs(writer_core, writer_artifacts)
        writer = tokenize_principal_obj(writer, vault)
    draft_path = _write_draft(
        vault, text, transcript_path, listener, writer, skeptic, interlocutor,
        task, mode, action, skeptic_approved,
    )
    record_inline_step("memory_pipeline.fanout")
    draft_rel = str(draft_path.relative_to(vault))
    # Entity stubs, decisions, and open loops are exempt from the skeptic gate
    # — they don't carry the same inference risk as state updates, evidence,
    # and claims (which encode the writer's interpretation as durable truth).
    frequent_names = _compute_frequent_names(vault, conversation_id)
    index_conn = open_index_connection(db_path)
    try:
        entities_touched = _create_entity_stubs(vault, writer, draft_rel, text, frequent_names=frequent_names, index_conn=index_conn)
        _create_relationship_edges(vault, writer, db_path=db_path, index_conn=index_conn)
        fanout_open_loops(vault, writer, draft_rel, source_text=text, index_conn=index_conn)
        fanout_decisions(vault, writer, draft_rel, source_text=text, index_conn=index_conn)
        if skeptic_approved:
            # Evidence runs before claims so claim.supporting_evidence can be
            # resolved through evidence_id_map (Finding 4). Claims run before the
            # state update so the state can reference resolved claim IDs in future
            # passes.
            evidence_id_map = fanout_evidence(vault, writer, transcript_path, draft_rel, index_conn=index_conn)
            fanout_claims(vault, writer, draft_rel, db_path=db_path, evidence_id_map=evidence_id_map, index_conn=index_conn)
            fanout_state_updates(vault, writer, draft_rel, index_conn=index_conn)
            _supersede_corrected_records(vault, writer, db_path=db_path)
        else:
            record_inline_step("memory_pipeline.fanout.skeptic_blocked")
        index_conn.commit()
    finally:
        index_conn.close()
    if skeptic_approved:
        _update_draft_status(draft_path, "fanout_applied")
    return MemoryPipelineResult(
        transcript_path=transcript_path,
        draft_path=draft_path,
        listener=listener,
        writer=writer,
        skeptic=skeptic,
        interlocutor=interlocutor,
        action=action,
        mode=mode,
        skeptic_approved=skeptic_approved,
        entities_touched=entities_touched,
    )


def _merge_writer_outputs(core: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Combine the episode_core and episode_artifacts JSON payloads (v0.1.9).

    The core call owns `summary`, `frontmatter`, `sections`, `claims_to_create`,
    etc.; the artifact call owns `entities_to_create`, `open_loops_to_create`,
    `decisions_to_create`, `state_updates`, `evidence_to_create`. We start
    from the core and overlay only the artifact arrays so the downstream
    fanout sees a single dict that looks identical to the legacy single-shot
    writer output.
    """
    merged = dict(core)
    artifact_keys = (
        "entities_to_create",
        "open_loops_to_create",
        "decisions_to_create",
        "state_updates",
        "evidence_to_create",
    )
    for key in artifact_keys:
        value = artifacts.get(key)
        if isinstance(value, list):
            merged[key] = value
    return merged


def route_turn(ctx: RoutingContext) -> RoutingDecision:
    """Apply the fixed routing cascade in order and preserve each mutation."""
    listener = dict(ctx.listener)
    action = str(listener.get("action", "skip"))
    mode = str(listener.get("mode", "skip"))
    applied_overrides: list[str] = []

    if (
        str(listener.get("memory_type") or "").lower() not in ("correction", "skip")
        and is_correction_turn(ctx.text)
    ):
        listener["memory_type"] = "correction"
        listener["reason"] = list(listener.get("reason") or []) + ["correction phrase detected"]
        applied_overrides.append("correction_override")

    seed_score = int(listener.get("seed_score", 0))
    if (
        action == "skip"
        and ctx.prior_state.mode_status not in ("closed",)
        and (ctx.prior_state.turn_count > 0 or seed_score > 0)
    ):
        action = "lightweight"
        mode = "elicitor"
        applied_overrides.append("never_skip_mid_conversation")

    transcript_turn_index = _conversation_turn_count(ctx.vault, ctx.conversation_id)
    if (
        action != "skip"
        and mode == "extraction"
        and transcript_turn_index <= 1
        and _has_distress_signal(listener, ctx.text)
    ):
        mode = "elicitor"
        applied_overrides.append("turn1_elicitor_preference")

    narrative_score = int(listener.get("narrative_score", 0))
    if action != "skip" and mode == "elicitor" and narrative_score >= 6:
        mode = "extraction"
        applied_overrides.append("narratively_complete_extraction")

    return RoutingDecision(
        listener=listener,
        action=action,
        mode=mode,
        applied_overrides=tuple(applied_overrides),
    )


def _choose_task(text: str, listener: dict[str, Any]) -> str:
    memory_type = str(listener.get("memory_type") or "").lower()
    if memory_type in ("decision", "open_loop", "state", "knowledge", "entity"):
        return memory_type
    if memory_type == "correction":
        return "state"
    return "episode"


_RECALL_QUESTION_PATTERNS = (
    r"\?",
    r"\b(remind me|do you remember|what do you remember|what did i|what was|what were|what's|what is)\b",
    r"\b(when did|when was|when is|where did|where is|who (?:did|is|was)|which|how many|how much|how long)\b",
    r"\b(tell me|look up|find|check)\b",
    r"\b(vendor|password|deadline|audit|meeting|appointment|plan|decision)\b",
)

_NON_RECALL_SKIP_PATTERNS = (
    r"\b(thanks|thank you|thx|ty)\b",
    r"\b(ok|okay|sure|got it|sounds good|all good|cool|great|nice)\b",
    r"\b(bye|goodbye|later|see ya|see you|talk later|heading out|signing off|nvm|never mind)\b",
)


def _normalize_skip_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_recall_query(text: str) -> bool:
    lowered = _normalize_skip_text(text)
    if not lowered:
        return False
    if _looks_like_non_recall_skip(lowered):
        return False
    return any(re.search(pattern, lowered) for pattern in _RECALL_QUESTION_PATTERNS)


def _looks_like_non_recall_skip(lowered: str) -> bool:
    if not lowered:
        return True
    if "?" in lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in _NON_RECALL_SKIP_PATTERNS)


def _closing_acknowledgment(text: str) -> str:
    lowered = _normalize_skip_text(text)
    if any(term in lowered for term in ("bye", "later", "see ya", "see you", "heading out", "signing off")):
        return "Talk soon."
    return "Okay."


def _build_skip_response(
    *,
    vault: Path,
    text: str,
    conversation_id: str | None,
    conversation_policy: dict[str, Any] | None,
    db_path: Path | None,
) -> str:
    domain_override = None
    if isinstance(conversation_policy, dict):
        domain_override = (
            conversation_policy.get("domain_override")
            or conversation_policy.get("arena_override")
        )

    # FIX B-1: only route to the recall answerer when the turn is plausibly a
    # request to retrieve something. A trivial farewell/acknowledgment ("ok
    # thanks, heading out. later.") is also action=="skip" and must NOT be told
    # "you didn't ask for a specific memory" — it gets a brief acknowledgment.
    # Deterministic-first gate (no extra LLM call); biased toward the answerer
    # for anything that isn't a clear closing.
    if not _is_recall_query(text):
        return _closing_acknowledgment(text)

    conn = open_index_connection(db_path)
    conn.close()

    result = retrieve_context(
        query=text,
        domain=str(domain_override) if domain_override else None,
        arena=str(domain_override) if domain_override else None,
        vault=vault,
        db_path=db_path,
        conversation_id=conversation_id,
    )
    items = _dedupe_retrieval_items(result.loaded)
    if not items:
        return "I don't have anything stored about that yet."

    # FIX B: a recall turn must *answer* the question from the retrieved records,
    # not dump raw summaries. Route through the Interlocutor (it already speaks to
    # the user and renders deixis), strictly grounded in the records with an
    # explicit no-fabrication instruction. FIX A: render the records to the
    # interlocutor audience first so {{principal}}/{{self}} never reach the user.
    return _answer_recall_from_records(
        vault=vault,
        question=text,
        items=items,
        conversation_policy=conversation_policy,
    )


def _render_recall_records(vault: Path, items: list[Any], limit: int = 8) -> list[str]:
    """Format retrieved records for the recall prompt, deixis-rendered for the
    user-facing audience ({{principal}}->"you", {{self}}->"I")."""
    records: list[str] = []
    for item in items[:limit]:
        summary = str(getattr(item, "summary", "") or "").strip() or str(getattr(item, "id", "") or "")
        summary = render_deixis(summary, "interlocutor")
        rtype = str(getattr(item, "type", "") or "record")
        records.append(f"[{rtype}] {summary}")
    return records


def _answer_recall_from_records(
    *,
    vault: Path,
    question: str,
    items: list[Any],
    conversation_policy: dict[str, Any] | None,
) -> str:
    """Generate a grounded recall answer via the Interlocutor.

    Reuses the InterlocutorAgent (decision made post-2026-06-19 eval): it is the
    answerer, strictly grounded in retrieved records, no fabrication, no external
    lookup. On any provider error or empty response we fall back to a rendered
    record list so a recall turn never fails the capture.
    """
    records = _render_recall_records(vault, items)
    recall_input = json.dumps(
        {
            "task": "answer_recall_question",
            "user_question": question,
            "retrieved_records": records,
            "instructions": (
                "The user is asking you to recall something from their stored memory. "
                "Answer their question directly and concisely using ONLY the retrieved records below. "
                "Quote the specifics they asked for — names, dates, numbers — when the records contain them. "
                "If the records do not contain the answer, say plainly that you don't have it stored; "
                "do NOT guess, infer, or invent any fact that is not in the records. "
                "Speak directly to the user as 'you'. Put your answer in the 'response' field."
            ),
        },
        indent=2,
        ensure_ascii=True,
    )
    response = ""
    try:
        out = InterlocutorAgent(vault=vault).run_json(
            recall_input,
            significance="medium",
            provider_error_mode="raise",
            conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
        )
        response = str(out.get("response") or "").strip()
    except Exception:
        response = ""
    if not response:
        # Defensive fallback (provider error / unusable response): a rendered
        # record list. Still no fabrication, and no raw role tokens.
        lines = ["Here's what I found in your stored records:"]
        for rec in records[:3]:
            lines.append(f"- {rec}")
        response = "\n".join(lines)
    # Belt-and-suspenders: render any tokens the model may have echoed back.
    return render_deixis(response, "interlocutor")


def _dedupe_retrieval_items(items: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(getattr(item, "id", "") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        unique.append(item)
    return unique


def _skeptic_approves(skeptic: dict[str, Any] | None) -> bool:
    """Gate state/evidence/claim fanout on skeptic approval.

    Block only when the skeptic explicitly holds a record — meaning it should
    not be stored yet. "revise" means "store with caveats", which we honour by
    proceeding; the issues list is preserved in the draft for human review.
    """
    if not isinstance(skeptic, dict):
        return True
    action = str(skeptic.get("recommended_action") or "").lower()
    if action in {"hold", "needs_revision"}:
        return False
    approved = skeptic.get("approved")
    # approved=False with no explicit hold action is treated as revise — proceed.
    # approved=False AND hold is already caught above.
    if approved is False and action not in {"approve", "revise", ""}:
        return False
    return True


def _interlocutor_input(
    writer: dict[str, Any],
    listener: dict[str, Any],
    prior_state: Any,
    user_text: str = "",
    vault: Path | None = None,
) -> dict[str, Any]:
    """Build a clean conversational payload — no skeptic notes, no internal flags."""
    memory_type = str(listener.get("memory_type") or "")
    is_correction = memory_type == "correction"
    # Deixis: the writer is *supposed* to store narrative in role tokens
    # ({{principal}}/{{self}}); the interlocutor speaks TO the principal, so render
    # those to second person ("you"/"I") here, before the payload reaches the
    # conversational agent. Weaker writer models frequently emit the principal's
    # literal NAME instead of the token, so we deterministically tokenize the
    # principal's aliases first (vault-scoped) and then render — without this
    # backstop an un-tokenized name leaks straight into the spoken reply. The
    # interlocutor never sees a name or a raw token. `entities` are genuine third
    # parties (verbatim) and `user_correction` is raw first-person user text
    # (verbatim). `writer_summary` is rendered even on correction turns because
    # it is writer-authored, not user-authored.
    def _i(text: str) -> str:
        t = text or ""
        if vault is not None:
            t = tokenize_principal(t, vault)
        return render_deixis(t, "interlocutor")

    payload: dict[str, Any] = {
        "writer_summary": _i(writer.get("summary") or ""),
        "writer_questions": writer.get("questions") or [],
        "memory_type": memory_type,
        "significance": writer.get("significance") or "medium",
        "entities": [e.get("name") for e in (writer.get("entities_to_create") or []) if isinstance(e, dict) and e.get("name")],
        "decisions": [_i(d.get("title")) for d in (writer.get("decisions_to_create") or []) if isinstance(d, dict) and d.get("title")],
        "open_loops": [_i(o.get("title")) for o in (writer.get("open_loops_to_create") or []) if isinstance(o, dict) and o.get("title")],
    }
    if is_correction:
        # On correction turns the prior narrative state predates the correction
        # and may actively contradict it — omit it to prevent the interlocutor
        # from echoing stale facts. Pass the raw user text instead so the
        # response can mirror the correction directly.
        payload["user_correction"] = user_text
        payload["narrative_state"] = {}
    else:
        payload["narrative_state"] = {
            "story_thread": _i(getattr(prior_state, "story_thread", "") or ""),
            "established": [_i(x) for x in (getattr(prior_state, "established", []) or [])],
            "open_threads": [_i(x) for x in (getattr(prior_state, "open_threads", []) or [])],
            "emotional_texture": _i(getattr(prior_state, "emotional_texture", "") or ""),
            "turn_count": getattr(prior_state, "turn_count", 0),
        }
    return payload


def _has_distress_signal(listener: dict[str, Any], text: str) -> bool:
    reasons = [str(r).lower() for r in (listener.get("reason") or [])]
    if any("affect" in r or "high-risk" in r or "biograph" in r for r in reasons):
        return True
    lowered = text.lower()
    distress_phrases = (
        "i don't know what to do", "i'm worried", "i'm scared", "i'm anxious",
        "freaking out", "falling apart", "can't handle", "feel awful",
        "hurts", "miss", "lonely", "overwhelmed", "stressed", "exhausted",
        # v0.1.7: distress can be third-person too (talking about a loved one)
        "sounded scared", "she's scared", "he's scared", "they're scared",
        "scared in a way", "shaken", "blindsided", "heavy",
        "i don't know what", "trying not to", "kept asking",
    )
    if any(term in lowered for term in distress_phrases):
        return True
    # Lone affect tokens are sufficient too — broader catch for short messages
    # that name an emotion without a full phrase ("I'm afraid", "feels heavy").
    distress_tokens = {
        "scared", "afraid", "fear", "fearful", "worried", "anxious",
        "panic", "terrified", "dread", "heartbroken", "devastated",
    }
    if any(token in lowered.split() for token in distress_tokens):
        return True
    return False


def _compute_frequent_names(vault: Path, conversation_id: str | None, threshold: int = 12) -> frozenset[str]:
    """Return capitalized first-name-shaped tokens that appear >= threshold times
    in today's transcript for conversation_id.

    Used as a fallback allowlist so first-name-only people who are mentioned
    repeatedly (e.g., Marcus across 30 turns) get entity files even when no
    explicit role label appears in the current turn.
    """
    if not conversation_id:
        return frozenset()
    from ..utils import today_iso as _today_iso
    today_transcript = vault / "transcripts" / f"{_today_iso()}.md"
    if not today_transcript.exists():
        return frozenset()
    try:
        full_text = today_transcript.read_text(encoding="utf-8")
    except Exception:
        return frozenset()
    # Extract only lines from this conversation's block.
    target_header = f"[{conversation_id}]"
    conv_lines: list[str] = []
    in_block = False
    for line in full_text.splitlines():
        if line.startswith("## Conversation — "):
            in_block = target_header in line
            continue
        if in_block:
            conv_lines.append(line)
    if not conv_lines:
        return frozenset()
    conv_text = " ".join(conv_lines)
    from collections import Counter
    from .stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS, DAY_STOPWORDS, MONTH_STOPWORDS
    words = re.findall(r"\b[A-Z][a-z]{2,}\b", conv_text)
    counts = Counter(words)
    return frozenset(
        word for word, count in counts.items()
        if count >= threshold
        and word not in SENTENCE_INITIAL_OR_TOOL_STOPWORDS
        and word not in DAY_STOPWORDS
        and word not in MONTH_STOPWORDS
    )


def _build_correction_context(text: str, db_path: Path | None = None) -> str:
    """FTS search for active claims/state records that may be superseded by this correction."""
    import sqlite3 as _sqlite3
    from ..paths import sqlite_path
    _db = db_path or sqlite_path()
    if not _db.exists():
        return ""
    tokens = [w for w in re.findall(r"\b[A-Za-z]{3,}\b", text) if w[0].isupper() and w not in {
        "I", "My", "The", "A", "An", "It", "He", "She", "They", "We", "You",
        "Actually", "Wait", "No", "Yes", "Not", "But", "And", "Or",
    }]
    if not tokens:
        return ""
    conn = _sqlite3.connect(_db)
    try:
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for token in tokens[:3]:
            try:
                results = conn.execute(
                    "SELECT f.id, f.type, f.summary, f.status FROM files f "
                    "JOIN files_fts fts ON f.id = fts.id "
                    "WHERE files_fts MATCH ? AND f.status = 'active' "
                    "AND f.type IN ('claim', 'state', 'episode') "
                    "ORDER BY bm25(files_fts) LIMIT 4",
                    (f'"{token}"',),
                ).fetchall()
            except _sqlite3.Error:
                continue
            for r in results:
                rid = r[0]
                if rid not in seen:
                    seen.add(rid)
                    rows.append({"id": rid, "type": r[1], "summary": r[2]})
        if not rows:
            return ""
        lines = ["## Possibly superseded records (correction context)"]
        lines.append("If the user is correcting one of these, include its ID in `corrects_ids`.")
        for r in rows[:5]:
            lines.append(f"- [{r['type']}] {r['id']}: {r['summary']}")
        return "\n".join(lines)
    finally:
        conn.close()


def _supersede_corrected_records(vault: Path, writer: dict[str, Any], db_path: Path | None = None) -> None:
    """Mark records listed in writer corrects_ids as superseded on disk."""
    corrects_ids = listify(writer.get("corrects_ids"))
    for record_id in corrects_ids:
        rid = str(record_id).strip()
        if rid:
            try:
                supersede_record(vault, rid, db_path=db_path)
            except Exception:
                pass


def _conversation_turn_count(vault: Path, conversation_id: str | None) -> int:
    """Count completed USER turns for `conversation_id` from today's transcript.

    v0.1.7: narrative state only increments on the elicitor path, so the
    extraction-only conversations end up with `turn_count` permanently at 0.
    Computing turn position deterministically from the transcript avoids that
    blind spot and lets the Turn-1 elicitor preference fire only once per
    conversation, not on every extraction turn that happens to carry affect.
    """
    if not conversation_id:
        return 0
    today_file = vault / "transcripts" / f"{today_iso()}.md"
    if not today_file.exists():
        return 0
    try:
        text = today_file.read_text(encoding="utf-8")
    except Exception:
        return 0
    marker = f"[{conversation_id}]"
    blocks = text.split("## Conversation — ")
    count = 0
    for block in blocks:
        if marker not in block:
            continue
        for line in block.splitlines():
            if line.strip().startswith("USER:"):
                count += 1
    return count


def _write_draft(
    vault: Path,
    text: str,
    transcript_path: Path,
    listener: dict[str, Any],
    writer: dict[str, Any],
    skeptic: dict[str, Any],
    interlocutor: dict[str, Any],
    task: str,
    mode: str,
    action: str,
    skeptic_approved: bool,
) -> Path:
    # Finding #6: the previous implementation used a live timestamp, so retries
    # of the same source text produced sibling draft files. Hashing the source
    # text makes the filename deterministic per turn; retries overwrite the
    # earlier draft instead of accumulating. today_iso() prefix preserves
    # uniqueness across days for the rare case of identical text on two days.
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    slug = slugify(str(writer.get("summary") or text[:48]))[:80]
    path = vault / "drafts" / f"{today_iso()}-{content_hash}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Finding 2: rejected drafts are held for Dreamer review with a distinct
    # status so the batch review process can find them.
    draft_status = "pending" if skeptic_approved else "needs_revision"
    writer_fm_raw = writer.get("frontmatter")
    writer_fm: dict[str, Any] = writer_fm_raw if isinstance(writer_fm_raw, dict) else {}
    frontmatter = {
        "id": f"draft.memory.{content_hash}.{slug}",
        "type": "draft",
        "created": today_iso(),
        "updated": today_iso(),
        "status": draft_status,
        "significance": str(writer.get("significance", "medium")),
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        # Finding #7: enforce word/sentence-boundary truncation on the
        # frontmatter summary too. The writer's working summary may be up to
        # 240 chars; the frontmatter convention is 120.
        "summary": _truncate_summary_boundary(
            str(writer.get("summary") or text), 120,
        ),
        "links": [str(transcript_path.relative_to(vault))],
        "confidence": str(writer_fm.get("confidence", "low")),
        "confidence_basis": str(writer_fm.get("confidence_basis", "Deterministic memory pipeline")),
        "last_confirmed": today_iso(),
        "review_after": today_iso(),
        "pipeline": {"action": action, "mode": mode, "task": task},
        "source": mode,
        "skeptic_approved": bool(skeptic_approved),
    }
    body = _render_draft_body(text, listener, writer, skeptic, interlocutor, task, skeptic_approved)
    write_markdown(path, with_domain_fields(frontmatter), body)
    return path


def _update_draft_status(path: Path, status: str) -> None:
    doc = load_markdown(path)
    frontmatter = dict(doc.frontmatter)
    frontmatter["status"] = status
    frontmatter["updated"] = today_iso()
    write_markdown(path, with_domain_fields(frontmatter), doc.body)


def _render_draft_body(
    text: str,
    listener: dict[str, Any],
    writer: dict[str, Any],
    skeptic: dict[str, Any],
    interlocutor: dict[str, Any],
    task: str,
    skeptic_approved: bool,
) -> str:
    status_note = (
        "Skeptic approved — fanout applied."
        if skeptic_approved
        else "Skeptic rejected — state/evidence/claim fanout was skipped. Held for Dreamer review."
    )
    return f"""# Memory Draft

## Status

{status_note}

## Task

{task}

## Listener

```json
{json.dumps(listener, indent=2, ensure_ascii=True)}
```

## Writer

```json
{json.dumps(writer, indent=2, ensure_ascii=True)}
```

## Skeptic

```json
{json.dumps(skeptic, indent=2, ensure_ascii=True)}
```

## Interlocutor

```json
{json.dumps(interlocutor, indent=2, ensure_ascii=True)}
```

## Source Text

{text.strip()}
"""


# ── Fanout: entities (extraction-only) ───────────────────────────────────────

def _create_entity_stubs(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    source_text: str,
    frequent_names: frozenset[str] | None = None,
    index_conn: Any | None = None,
) -> list[Path]:
    """Materialize entity stubs proposed by the writer.

    Returns paths for all entities processed (new or existing) so callers
    can enqueue story-rewrite jobs for entities that received new material.
    """
    from .primer_index import known_names as _primer_known_names
    from .primer_index import roster as _roster
    from .entity_kind import assign_kind

    entities = writer.get("entities_to_create") or []
    if not entities:
        return []
    index = _load_entity_index(vault)
    primer_cast = _primer_known_names(vault)
    # Acceptance allowlist = primer cast + roster (known entities of ANY kind) +
    # frequently-mentioned names. Seeding the roster here also kills the
    # duplicate-invention problem at the source (spec §4 Layer 1).
    roster_names: set[str] = set()
    for _entry in _roster(vault):
        roster_names.add(_entry.name)
        roster_names.update(_entry.aliases)
    allowlist = primer_cast | (frequent_names or frozenset()) | frozenset(roster_names)
    seen_in_pass: set[str] = set()
    entities_touched_set: set[Path] = set()
    for entry in entities:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not name:
            continue
        # FIX A: {{principal}}/{{self}} are deixis ROLES, not entities. A writer
        # that emits the role token (or its bare slug) as an entity name must
        # never materialize a record for it — that produced the bogus
        # entities/events/principal.md in the 2026-06-19 eval. Drop any
        # candidate whose name carries a role token or is a bare role slug.
        if has_unresolved_token(name) or name.strip().lower() in {"principal", "self", "user"}:
            continue
        normalized = name.lower()
        if normalized in seen_in_pass:
            continue
        seen_in_pass.add(normalized)

        # Kind (P3): roster -> structural -> model's explicit choice -> thing.
        # NEVER defaults to person — that was the Atlas/Houston bug. The result
        # is stored as both `kind` and `subtype` (see new_entity) and scopes
        # dedup so a person "Atlas" and a project "Atlas" never merge.
        subtype = assign_kind(
            name,
            vault,
            model_kind=str(entry.get("kind") or entry.get("subtype") or "").strip(),
            summary=summary,
            source_text=source_text,
        )
        if not subtype:
            continue

        pronoun_reject = {"she", "he", "they", "her", "him", "them", "it", "we", "i", "me", "us"}
        if normalized in pronoun_reject:
            continue
        if not _looks_like_entity(name, subtype, allowlist, source_text):
            continue

        raw_aliases = entry.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
        user_handle = _scan_user_stated_handle(name, source_text, {alias.lower() for alias in aliases})
        if not user_handle:
            user_handle = next((alias for alias in aliases if alias.lower() != name.lower()), None)
        if user_handle and user_handle.lower() not in {alias.lower() for alias in aliases}:
            aliases.append(user_handle)

        existing = _match_existing_entity(vault, name, subtype, index, allowlist, source_text, summary=summary)
        if existing is not None:
            _append_entity_alias(existing, name)
            for alias in aliases:
                _append_entity_alias(existing, alias)
            if user_handle:
                _assign_entity_nickname(existing, user_handle)
            index_created_record(vault, CreatedRecord(path=existing, created=True), index_conn)
            entities_touched_set.add(existing)
            # Refresh the in-memory index so the next sibling in the same pass
            # also resolves to this canonical entity. Full-name key only —
            # surname tokens stay subject to the strict-token rule.
            index.setdefault(name.lower(),
                             {"path": existing, "kind": "full", "canonical": name})
            continue
        try:
            same_first_records = []
            nickname = None
            if subtype == "person":
                same_first_records = _same_first_name_records(vault, name, subtype, index)
                if same_first_records:
                    assigned_nicknames = _ensure_nicknames_for_collision(vault, same_first_records, source_text=source_text)
                    existing_handles = {
                        str(value).strip().lower()
                        for _, fm, _ in same_first_records
                        for value in _entity_identity_names(fm)
                    }
                    existing_handles.update(str(nickname).strip().lower() for nickname in assigned_nicknames.values())
                    nickname = _entity_nickname(
                        name,
                        summary=summary,
                        source_text=source_text,
                        existing_handles=existing_handles,
                    )
                if not nickname and user_handle:
                    nickname = user_handle
            created = new_entity(
                vault=vault,
                name=name,
                subtype=subtype,
                summary=summary or f"{name} mentioned in conversation.",
                confidence="low",
                confidence_basis=basis_or_default(entry, "Auto-extracted from conversation"),
                aliases=aliases,
                nickname=nickname,
                disambiguation=_entity_disambiguator_from_candidates(vault, name, subtype, index, summary, source_text),
            )
            index_created_record(vault, created, index_conn)
            entities_touched_set.add(created.path)
            if nickname:
                index.setdefault(nickname.lower(), {"path": created.path, "kind": "full", "canonical": name})
                for token in nickname.split():
                    tkey = token.lower()
                    existing_entry = index.get(tkey)
                    if existing_entry is None:
                        index[tkey] = {"path": created.path, "kind": "token", "canonical": name}
                    elif existing_entry.get("path") != created.path and existing_entry.get("kind") == "token":
                        existing_entry["kind"] = "ambiguous"
        except FileExistsError:
            continue
        # Finding #5: when seeding the index after a creation, register only
        # the full canonical name as a "full" hit and each token as "token".
        # If a second entity later tries to claim the same token, the index
        # marks it ambiguous and the strict matcher refuses cross-merges.
        index.setdefault(name.lower(),
                         {"path": created.path, "kind": "full", "canonical": name})
        for token in name.split():
            tkey = token.lower()
            existing_entry = index.get(tkey)
            if existing_entry is None:
                index[tkey] = {"path": created.path, "kind": "token", "canonical": name}
            elif existing_entry.get("path") != created.path and existing_entry.get("kind") == "token":
                existing_entry["kind"] = "ambiguous"
    return list(entities_touched_set)


def _create_relationship_edges(
    vault: Path,
    writer: dict[str, Any],
    db_path: Path | None = None,
    index_conn: Any | None = None,
) -> None:
    """Write entity-to-entity relationship edges from writer relationships_to_create."""
    import sqlite3 as _sqlite3
    from ..paths import sqlite_path
    relationships = list(writer.get("relationships_to_create") or [])
    if not relationships:
        return
    _db = db_path or sqlite_path()
    if index_conn is None and not _db.exists():
        return
    conn = index_conn or _sqlite3.connect(_db)
    try:
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            entity_a = str(rel.get("entity_a") or "").strip()
            entity_b = str(rel.get("entity_b") or "").strip()
            rel_type = str(rel.get("relationship_type") or "related_to").strip()
            if not entity_a or not entity_b:
                continue
            # Resolve names to entity IDs via the alias table.
            row_a = conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ? LIMIT 1",
                (entity_a,),
            ).fetchone()
            row_b = conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ? LIMIT 1",
                (entity_b,),
            ).fetchone()
            if not row_a or not row_b:
                continue
            id_a, id_b = row_a[0], row_b[0]
            existing = conn.execute(
                "SELECT 1 FROM links WHERE source_id=? AND target_id=? AND relationship_type=?",
                (id_a, id_b, rel_type),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO links (source_id, target_id, relationship_type) VALUES (?, ?, ?)",
                    (id_a, id_b, rel_type),
                )
        if index_conn is None:
            conn.commit()
    except _sqlite3.Error:
        pass
    finally:
        if index_conn is None:
            conn.close()


def _normalize_entity_subtype(
    *,
    name: str,
    subtype: str,
    summary: str,
    source_text: str,
    primer_cast: frozenset[str],
) -> str | None:
    """Coerce writer-emitted subtype labels into one of the supported buckets."""
    allowed = frozenset({"person", "place", "thing", "project", "organization"})
    subtype = (subtype or "person").strip().lower()
    if subtype in allowed:
        if subtype == "person" and _looks_like_organization(name, summary, source_text, primer_cast):
            return "organization"
        return subtype
    if _looks_like_organization(name, summary, source_text, primer_cast):
        return "organization"
    return "person"


def _looks_like_organization(
    name: str,
    summary: str,
    source_text: str,
    primer_cast: frozenset[str],
) -> bool:
    """Heuristic for company / org-like entities that should not be typed as people."""
    combined = " ".join(part for part in (name, summary, source_text) if part).lower()
    org_markers = (
        "company",
        "employer",
        "organization",
        "organisation",
        "corporation",
        "corporate",
        "startup",
        "firm",
        "business",
        "vendor",
        "contractor",
        "department",
        "division",
        "group",
        "team",
        "studio",
        "labs",
        "systems",
        "solutions",
        "holdings",
        "ventures",
        "partners",
        "works at",
        "work for",
        "employed at",
    )
    if any(marker in combined for marker in org_markers):
        if len(name.split()) >= 2:
            return True
    lower_name = name.lower()
    if any(lower_name.endswith(suffix) for suffix in (" inc", " llc", " ltd", " corp", " company", " group", " labs", " systems", " studio", " ventures", " holdings", " partners")):
        return True
    return False


# Professional titles that unambiguously prefix a person's name.
_PERSON_TITLES: frozenset[str] = frozenset({
    "dr", "mr", "mrs", "ms", "miss", "prof", "rev",
    "sgt", "cpl", "cpt", "capt", "lt", "col", "gen", "adm",
})

# Tokens that are never a person's name, regardless of context.
# Scoped to the person branch only — other kinds (organization, thing, place) are unaffected.
#
# Deliberately excludes day names and month names so that context-named persons
# like "Tuesday", "January", "August", "May" resolve via _has_person_role_context
# ("my friend Tuesday", "I went out with January", "my colleague August").
# Any word can be a name when the surrounding structure says so; this list
# captures only tokens that have NO plausible person sense.
_NEVER_PERSON_TOKENS: frozenset[str] = frozenset({
    # Determiners, pronouns, conjunctions, prepositions
    "The", "A", "An", "It", "He", "She", "They", "We", "You",
    "His", "Her", "Their", "Our", "My", "Me", "Mine", "I",
    "No", "Yes", "Ok", "Okay", "So", "But", "And", "Or",
    "In", "On", "At", "Of", "For", "With", "From", "By", "Up", "Out",
    # Interrogatives and sentence-initial adverbs
    "What", "Why", "How", "When", "Where", "Who", "Whom", "Whose", "Which",
    "Then", "Now", "Today", "Tomorrow", "Yesterday",
    "Strategically", "Honestly", "Frankly", "Maybe", "Perhaps", "Probably",
    "Anyway", "Actually", "Eventually", "Finally", "Basically", "Apparently",
    "Hopefully", "Obviously", "Clearly", "Suddenly", "Recently",
    # Productivity tools / platforms (clearly never persons)
    "Slack", "Zoom", "GitHub", "Gmail", "Notion", "Jira", "Linear",
    "Google", "Microsoft", "Apple", "Discord", "Figma", "Trello",
    "Asana", "Confluence", "Outlook", "Teams", "Dropbox", "OneDrive",
    "Excel", "Word", "PowerPoint", "Sheets", "Docs", "Calendar",
    "YouTube", "Twitter", "Reddit", "Facebook", "Instagram",
    "ChatGPT", "Claude", "OpenAI", "Anthropic",
    # Dating / social apps — person sense is implausible even with social context
    "Bumble", "Hinge", "Tinder", "OkCupid",
})

# Relationship/role words that, when found near a first name, confirm the
# name refers to a real person even though it is a single token.
_RELATIONSHIP_WORDS: frozenset[str] = frozenset({
    # Family
    "son", "daughter", "dad", "mom", "mother", "father", "brother", "sister",
    "husband", "wife", "partner", "uncle", "aunt", "grandpa", "grandma",
    "grandfather", "grandmother", "grandson", "granddaughter", "nephew",
    "niece", "cousin", "stepmom", "stepdad", "stepson", "stepdaughter",
    "fiance", "fiancee", "ex",
    # Casual / informal
    "buddy", "pal", "bro", "bestie", "homie", "mate",
    "date",  # "my date Friday" — date as a person, not a calendar day
    "guy", "dude", "crush",
    "barber", "stylist", "trainer", "instructor", "tutor",
    "landlord", "tenant",
    "babysitter", "nanny",
    "vet",  # "my vet Dr. March"
    # Professional / social
    "colleague", "coworker", "boss", "manager", "supervisor", "therapist",
    "lawyer", "attorney", "accountant", "mentor", "coach", "advisor",
    "friend", "neighbor", "roommate", "classmate", "teammate",
    "boyfriend", "girlfriend", "doctor",
})

_EVENT_PHRASE = re.compile(
    r"^(?:dinner|lunch|brunch|breakfast|drinks|coffee|happy\s+hour|meeting|check-?in|"
    r"appointment|session|practice|rehearsal|game|party|gathering|cookout|barbecue|bbq)"
    r"\s+"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tonight|tomorrow|today|morning|afternoon|evening|night|weekly|daily)",
    re.IGNORECASE,
)

_EVENT_PHRASE_TIME_FIRST = re.compile(
    r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tonight|tomorrow|today|morning|afternoon|evening|night|weekly|daily)"
    r"\s+"
    r"(?:check-?in|meeting|appointment|session|practice|rehearsal|game|party|gathering|"
    r"cookout|barbecue|bbq|dinner|lunch|brunch|breakfast|drinks|coffee|happy\s+hour)",
    re.IGNORECASE,
)

_PLACE_PHRASE = re.compile(
    r"^(?:north|south|east|west|upper|lower|old|new|downtown|midtown|uptown|central|"
    r"lake|river|park|mount|fort|port|bay|st\.?|saint)\s+\w+",
    re.IGNORECASE,
)


def _has_person_role_context(name: str, source_text: str) -> bool:
    """Return True when the source text places *name* in a clear person context.

    Detects four pattern families:
      - possessive-role-name: "my/his/her/their [role] [Name]"
      - name-role appositive:  "[Name], my/his/her [role]" or "[Name] is my [role]"
      - name-as-agent: "[Name] texted/called/messaged/emailed me"
      - social-action:  "I/we went out with [Name]", "I met/saw [Name]",
                        "dinner/lunch/drinks/coffee with [Name]"
    """
    if not source_text:
        return False
    lowered = source_text.lower()
    name_lower = name.lower()
    n = re.escape(name_lower)
    role_group = "(?:" + "|".join(re.escape(w) for w in _RELATIONSHIP_WORDS) + ")"

    possessive = r"(?:my|his|her|their|our)\s+(?:\w+\s+)?" + role_group + r"\s+" + n
    appositive = n + r"(?:,?\s+(?:my|his|her|their)\s+" + role_group + r"|\s+is\s+(?:my|his|her|their)\s+" + role_group + r")"
    # "Her name is Barbara", "my name is Barbara", "this is Barbara"
    intro_named = (
        r"(?:my|his|her|their|our)\s+name\s+is\s+" + n
        + r"|(?:this|that)\s+is\s+" + n
        + r"|(?:met\s+(?:someone\s+)?named|someone\s+named)\s+" + n
        + r"|(?:named|called|known\s+as|goes\s+by)\s+" + n
    )
    # "[Name] texted/called/messaged me" — name acting as a communicating person
    name_acts = n + r"\s+(?:texted|called|messaged|emailed|reached\s+out|pinged|wrote|rang)"
    # "I/we texted/called/met/saw [Name]"
    i_act_name = r"(?:i|we)\s+(?:texted|called|messaged|emailed|met|saw|visited|asked|told)\s+(?:\w+\s+){0,3}" + n
    # "went (out) with [Name]", "dinner/lunch/drinks/coffee with [Name]"
    social_with = (
        r"(?:went\s+(?:out\s+)?with"
        r"|(?:had\s+)?(?:dinner|lunch|drinks|coffee|brunch)\s+with"
        r"|a\s+date\s+with"
        r"|talking\s+to|talked\s+to|speaking\s+with|spoke\s+with"
        r")\s+(?:\w+\s+){0,4}" + n
    )
    return bool(
        re.search(possessive, lowered)
        or re.search(appositive, lowered)
        or re.search(intro_named, lowered)
        or re.search(name_acts, lowered)
        or re.search(i_act_name, lowered)
        or re.search(social_with, lowered)
    )


def _looks_like_entity(name: str, subtype: str, primer_cast: frozenset[str], source_text: str = "") -> bool:
    """Validate that *name* is plausibly an entity of *subtype*.

    Rules (in priority order):
    1. Primer/roster-known names: always accepted (highest authority).
    2. Title-prefixed names ("Dr. Kwan", "Ms. Reyes"): always persons.
    3. Single-token persons:
       a. Hard reject if in _NEVER_PERSON_TOKENS (function words, platform names).
       b. Otherwise, accept only when _has_person_role_context fires — this lets
          day names, month names, seasons, and other "name-that-is-also-a-word"
          tokens resolve as persons when structural context supports it
          ("my friend Tuesday", "I went out with January", "my colleague August").
    4. Multi-token persons: reject if any token is a function word/platform; require
       all tokens to be proper-noun shaped (uppercase-initial). Day and month names
       are allowed as name components ("Tuesday Smith", "August Chen").
    5. Non-person subtypes: light-touch validation only.
    """
    from .stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS

    if not name:
        return False

    if name in primer_cast:
        return True

    tokens = name.split()
    if not tokens:
        return False

    if subtype == "person":
        # Title-prefixed names ("Dr. Kwan", "Ms. Reyes") are always persons.
        first_token_bare = tokens[0].rstrip(".").lower()
        if first_token_bare in _PERSON_TITLES and len(tokens) >= 2:
            return True

        if len(tokens) < 2:
            # Hard reject: function words and platform names that can never be
            # person names. Days, months, seasons, and common-word names are NOT
            # in this set — they are context-gated below so that persons named
            # Tuesday, January, August, Mercury, Summer, etc. can still resolve.
            if name in _NEVER_PERSON_TOKENS:
                return False
            return _has_person_role_context(name, source_text)

        # Multi-token names: reject if any token is a function word or platform;
        # allow day/month names as valid name components ("Tuesday Smith").
        for tok in tokens:
            if tok in _NEVER_PERSON_TOKENS:
                return False
        if not all(t[:1].isupper() and len(t) > 1 for t in tokens):
            return False
        combined = " ".join(tokens)
        if _EVENT_PHRASE.match(combined) or _EVENT_PHRASE_TIME_FIRST.match(combined) or _PLACE_PHRASE.match(combined):
            return False
        return True

    # Non-person subtypes: light-touch validation.
    if name in SENTENCE_INITIAL_OR_TOOL_STOPWORDS and name not in primer_cast:
        return False
    return True


def _load_entity_index(vault: Path) -> dict[str, dict[str, Any]]:
    """Map names and tokens to entity records, keeping them distinguishable.

    Finding #5: a previous version flattened "full canonical name" and
    "individual token" lookups onto the same path, so a surname-only token hit
    looked identical to a full-name hit. We now mark each entry as ``"full"``,
    ``"token"``, or ``"ambiguous"`` (when two entities both claim the same
    token), and ``_match_existing_entity`` reads those flags to decide whether
    a merge is safe.
    """
    index: dict[str, dict[str, Any]] = {}
    entities_root = vault / "entities"
    if not entities_root.exists():
        return index
    for path in entities_root.rglob("*.md"):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        canonical = str(doc.frontmatter.get("canonical_name") or "").strip()
        nickname = str(doc.frontmatter.get("nickname") or "").strip()
        aliases = doc.frontmatter.get("aliases") or []
        names = [canonical, nickname] + [str(a) for a in aliases if isinstance(a, str)]
        for name in names:
            if not name:
                continue
            key = name.lower()
            existing = index.get(key)
            if existing is None:
                index[key] = {"path": path, "kind": "full", "canonical": canonical or name}
            elif existing.get("path") != path:
                existing["kind"] = "ambiguous"
            for token in name.split():
                tkey = token.lower()
                existing = index.get(tkey)
                if existing is None:
                    index[tkey] = {"path": path, "kind": "token", "canonical": canonical or name}
                elif existing.get("path") != path and existing.get("kind") == "token":
                    # Two distinct entities want the same surname token. Mark
                    # the entry ambiguous so single-token merges are refused.
                    existing["kind"] = "ambiguous"
    return index


def _entity_resolution_candidates(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    from .reference_resolution import candidate_keys

    tokens = {token.lower() for token in name.split() if token}
    if not tokens:
        return []
    candidates: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path) or path in seen_paths:
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        payload = dict(doc.frontmatter)
        payload["path"] = path
        payload["body"] = doc.body
        candidate_tokens = candidate_keys(payload)
        if candidate_tokens.intersection(tokens) or normalize_text(payload.get("canonical_name") or "") == normalize_text(name):
            candidates.append(payload)
            seen_paths.add(path)
    if candidates:
        return candidates
    for path in (vault / "entities").rglob("*.md"):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type") or "") != "entity":
            continue
        if str(doc.frontmatter.get("subtype") or "") != subtype:
            continue
        payload = dict(doc.frontmatter)
        payload["path"] = path
        payload["body"] = doc.body
        candidate_tokens = candidate_keys(payload)
        if candidate_tokens.intersection(tokens) or normalize_text(payload.get("canonical_name") or "") == normalize_text(name):
            candidates.append(payload)
    return candidates


def _entity_disambiguator(name: str, summary: str, source_text: str) -> str | None:
    tokens = []
    combined = " ".join(part for part in (summary, source_text) if part).strip().lower()
    if not combined:
        return None
    exclude = {token.lower() for token in name.split() if token}
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+", combined):
        if len(token) <= 3 or token in exclude:
            continue
        if token in {"this", "that", "with", "from", "into", "over", "under", "about", "after", "before", "kept", "named"}:
            continue
        tokens.append(token)
    return tokens[0] if tokens else None


_NICKNAME_HINTS: list[tuple[str, str]] = [
    ("guitar", "Guitar"),
    ("studio", "Studio"),
    ("music", "Music"),
    ("accountant", "Accountant"),
    ("budget", "Budget"),
    ("tax", "Tax"),
    ("lunch", "Lunch"),
    ("office", "Office"),
    ("gym", "Gym"),
    ("meeting", "Meeting"),
    ("project", "Project"),
    ("family", "Family"),
    ("work", "Work"),
    ("coffee", "Coffee"),
    ("school", "School"),
    ("clinic", "Clinic"),
    ("therapy", "Therapy"),
]

_NICKNAME_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "in", "into", "is", "it", "its",
    "me", "my", "of", "on", "or", "our", "she", "that", "the", "their", "them", "there",
    "they", "this", "to", "was", "we", "with", "you", "your", "who", "what", "when",
    "where", "why", "how", "record", "records", "handle", "handles", "working", "works",
    "said", "says", "say", "doing", "do", "did", "done", "directly", "named",
    # D1a: deixis role tokens — strip {{principal}} → "principal" etc. from roots
    "principal", "self", "user",
}


def _pascalize_token(token: str) -> str:
    parts = [part for part in re.split(r"[-_ ]+", str(token).strip()) if part]
    return "".join(part[:1].upper() + part[1:].lower() for part in parts)


def _entity_first_token(name: str) -> str:
    token = str(name or "").strip().split()[0] if str(name or "").strip() else ""
    return token.lower()


def _entity_name_roots(*values: str) -> list[str]:
    combined = " ".join(str(value or "") for value in values).strip().lower()
    roots: list[str] = []
    seen: set[str] = set()
    for needle, label in _NICKNAME_HINTS:
        if needle in combined and label not in seen:
            seen.add(label)
            roots.append(label)
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+", combined):
        if len(token) <= 3 or token in _NICKNAME_STOPWORDS:
            continue
        root = _pascalize_token(token)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    if not roots:
        roots.extend(["Context", "Signal", "Thread", "Marker"])
    return roots


# D1b: prefix patterns that mark the start of a user-stated handle declaration.
# Case-insensitive for the trigger phrase; the nickname itself is extracted from
# the text *after* the match end using _CAPITALIZED_WORDS (case-sensitive anchor)
# so trailing lowercase clause words ("because", "so", ...) are never captured.
_USER_HANDLE_PREFIXES: list[re.Pattern[str]] = [
    # "I call her …", "we've been calling him …"
    re.compile(r"(?i)(?:i|we)(?:'ve)?\s+(?:been\s+)?call(?:ed|ing)?\s+(?:her|him|them|it)\s+"),
    # "goes by …"
    re.compile(r"(?i)goes\s+by\s+"),
    # "(her/his/their/my) nickname is …"
    re.compile(r"(?i)(?:her|his|their|my)?\s*nickname\s+(?:is|was)\s+"),
    # "aka …"
    re.compile(r"(?i)\baka\b\s+"),
    # "also known as …"
    re.compile(r"(?i)also\s+known\s+as\s+"),
]

# Extracts 1-4 consecutive Title-Cased words from the start of a string.
_CAPITALIZED_WORDS: re.Pattern[str] = re.compile(
    r"[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,3}"
)

_HANDLE_WINDOW = 400  # chars: how close a stated handle must be to the person's first name


def _scan_user_stated_handle(
    name: str,
    source_text: str,
    existing_handles: set[str],
) -> str | None:
    """Return the first user-stated nickname for *name* found near it in *source_text*.

    Searches for Tier-1 explicit declaration patterns ("I call her X",
    "goes by X", "aka X") within _HANDLE_WINDOW characters of any occurrence
    of the person's first name. Returns the handle verbatim (as the user wrote
    it) if it's not already taken by another entity.
    """
    if not source_text:
        return None
    first = _entity_first_token(name)
    if not first:
        return None
    text_lower = source_text.lower()
    first_positions = [
        m.start()
        for m in re.finditer(r"\b" + re.escape(first) + r"\b", text_lower)
    ]
    if not first_positions:
        return None
    for prefix_pat in _USER_HANDLE_PREFIXES:
        for m in prefix_pat.finditer(source_text):
            # Extract capitalized-word run starting at the end of the trigger phrase.
            cap = _CAPITALIZED_WORDS.match(source_text, m.end())
            if not cap:
                continue
            raw = cap.group(0).strip()
            if not raw:
                continue
            if not any(abs(m.start() - pos) <= _HANDLE_WINDOW for pos in first_positions):
                continue
            if raw.lower() not in existing_handles:
                return raw
    return None


def _entity_nickname(
    name: str,
    *,
    summary: str = "",
    source_text: str = "",
    existing_handles: set[str] | None = None,
) -> str | None:
    first = _pascalize_token(_entity_first_token(name))
    if not first:
        return None
    handles = {str(item).strip().lower() for item in (existing_handles or set()) if str(item).strip()}
    # D1b Tier 1: user-stated handle wins over any system-coined nickname.
    user_handle = _scan_user_stated_handle(name, source_text, handles)
    if user_handle:
        return user_handle
    for root in _entity_name_roots(summary, source_text):
        nickname = f"{root}{first}"
        if nickname.lower() not in handles:
            return nickname
    return None


def _entity_identity_names(fm: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("canonical_name", "nickname", "disambiguation"):
        value = str(fm.get(field) or "").strip()
        if value:
            values.append(value)
    values.extend(str(alias).strip() for alias in listify(fm.get("aliases")))
    return [value for value in values if value]


def _same_first_name_records(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
) -> list[tuple[Path, dict[str, Any], str]]:
    first = _entity_first_token(name)
    if not first:
        return []
    records: list[tuple[Path, dict[str, Any], str]] = []
    seen: set[Path] = set()
    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path) or path in seen:
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = dict(doc.frontmatter)
        names = _entity_identity_names(fm)
        if not any(_entity_first_token(value) == first for value in names):
            continue
        seen.add(path)
        records.append((path, fm, doc.body))
    return records


def _assign_entity_nickname(
    path: Path,
    nickname: str,
) -> None:
    try:
        doc = load_markdown(path)
    except Exception:
        return
    fm = dict(doc.frontmatter)
    if str(fm.get("nickname") or "").strip() == nickname:
        return
    fm["nickname"] = nickname
    fm["updated"] = today_iso()
    write_markdown(path, fm, doc.body)


def _ensure_nicknames_for_collision(
    vault: Path,
    records: list[tuple[Path, dict[str, Any], str]],
    *,
    source_text: str = "",
) -> dict[Path, str]:
    assigned: dict[Path, str] = {}
    existing_handles: set[str] = set()
    for _, fm, _ in records:
        existing_handles.update(str(value).strip().lower() for value in _entity_identity_names(fm))
    for path, fm, body in sorted(records, key=lambda item: str(item[0])):
        if str(fm.get("nickname") or "").strip():
            existing_handles.add(str(fm.get("nickname") or "").strip().lower())
            continue
        nickname = _entity_nickname(
            str(fm.get("canonical_name") or fm.get("id") or path.stem),
            summary=str(fm.get("summary") or body or ""),
            source_text=source_text or body,
            existing_handles=existing_handles,
        )
        if not nickname:
            continue
        _assign_entity_nickname(path, nickname)
        assigned[path] = nickname
        existing_handles.add(nickname.lower())
    return assigned


def _entity_disambiguator_from_candidates(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
    summary: str,
    source_text: str,
) -> str | None:
    candidates = _entity_resolution_candidates(vault, name, subtype, index)
    if not candidates:
        return None
    return _entity_disambiguator(name, summary, source_text)


def _match_existing_entity(
    vault: Path,
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
    primer_cast: frozenset[str] | None = None,
    source_text: str = "",
    *,
    summary: str = "",
) -> Path | None:
    """Find an entity that this proposed name should fold into, if any.

    Finding #5 rules:
    - Full-name match (case-insensitive) → merge if subtype matches.
    - Single-word proposal → merge only if the token is unambiguous.
    - Multi-word proposal → require >= 2 shared tokens with the same target,
      or a single shared token whose entry resolves to the same primer-cast
      canonical as the proposal.
    """
    primer_cast = primer_cast or frozenset()

    direct = index.get(name.lower())
    if direct and direct.get("kind") == "full" and _entity_subtype(direct["path"]) == subtype:
        return direct["path"]
    if direct and direct.get("kind") == "full":
        direct_path = direct.get("path")
        if isinstance(direct_path, Path) and _entity_subtype(direct_path) == "person":
            return direct_path

    tokens = [t.lower() for t in name.split() if t]
    if not tokens:
        return None

    if len(tokens) == 1:
        # Single-word proposal can absorb into an existing multi-word entity
        # only when exactly one entity claims that token.
        entry = index.get(tokens[0])
        if entry is not None and entry.get("kind") in ("token", "full") and entry.get("kind") != "ambiguous" and _entity_subtype(entry["path"]) == subtype:
            return entry["path"]
        if entry is not None and entry.get("kind") in ("token", "full") and entry.get("kind") != "ambiguous":
            entry_path = entry.get("path")
            if isinstance(entry_path, Path) and _entity_subtype(entry_path) == "person":
                return entry_path
        candidates = _entity_resolution_candidates(vault, name, subtype, index)
        if not candidates:
            return None
        neighborhood = " ".join(part for part in (summary, source_text, name) if part).strip()
        result = resolve_reference(neighborhood, candidates)
        if resolution_action(result.confidence, load_bearing=True) == "bind" and result.candidate is not None:
            path = result.candidate.get("path")
            if isinstance(path, Path):
                return path
        return None

    for entry in index.values():
        path = entry.get("path")
        if not isinstance(path, Path):
            continue
        if _entity_subtype(path) != subtype:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        candidate = dict(doc.frontmatter)
        candidate["path"] = path
        candidate["body"] = doc.body
        if _candidate_has_surname_conflict(name, candidate):
            return None

    # Multi-word proposal: tally per-target token hits.
    hits: dict[Path, int] = {}
    for tok in tokens:
        entry = index.get(tok)
        if not entry or entry.get("kind") == "ambiguous":
            continue
        if _entity_subtype(entry["path"]) != subtype:
            continue
        hits[entry["path"]] = hits.get(entry["path"], 0) + 1
    # Require >= 2 token overlap for a multi-word merge.
    for path, n in hits.items():
        if n >= 2:
            return path
    candidates = _entity_resolution_candidates(vault, name, subtype, index)
    if candidates:
        neighborhood = " ".join(part for part in (summary, source_text, name) if part).strip()
        result = resolve_reference(neighborhood, candidates)
        if resolution_action(result.confidence, load_bearing=True) == "bind" and result.candidate is not None:
            path = result.candidate.get("path")
            if isinstance(path, Path):
                return path
    if subtype != "person":
        # Canonical-person safeguard: if a human with this exact slug already
        # exists anywhere in the vault, prefer that record over a context-leaked
        # non-person subtype. This prevents people introduced in event turns
        # from spawning shadow records under entities/events/.
        slug = slugify(name)
        for path in (vault / "entities").rglob(f"{slug}.md"):
            if _entity_subtype(path) == "person":
                return path
    # Optional primer-cast tiebreaker: if there's exactly one single-token
    # hit *and* the proposed name is in the primer cast and the existing
    # entity's canonical is also in the primer cast under a different name,
    # refuse the merge (they are distinct primer-cast members).
    if hits and primer_cast and name in primer_cast:
        # Two primer-cast members with a shared surname: never merge.
        return None
    return None


def _entity_subtype(path: Path) -> str:
    try:
        return str(load_markdown(path).frontmatter.get("subtype") or "")
    except Exception:
        return ""


def _entity_name_tokens(name: str) -> list[str]:
    return [token.lower() for token in str(name or "").split() if token]


def _candidate_has_surname_conflict(name: str, candidate: dict[str, Any]) -> bool:
    proposal_tokens = _entity_name_tokens(name)
    if len(proposal_tokens) < 2:
        return False
    proposal_surname = proposal_tokens[-1]
    identity_haystack = " ".join(
        str(candidate.get(field) or "").strip()
        for field in ("canonical_name", "nickname", "disambiguation", "summary")
    ).lower()
    identity_names = _entity_identity_names(candidate)
    candidate_surnames = {
        tokens[-1]
        for tokens in (_entity_name_tokens(value) for value in identity_names)
        if len(tokens) >= 2
    }
    if proposal_surname in candidate_surnames:
        return False
    if candidate_surnames:
        return True
    return proposal_surname not in identity_haystack


def _append_entity_alias(path: Path, alias: str) -> None:
    """Add `alias` to the entity record at `path` if it's not already present."""
    try:
        doc = load_markdown(path)
    except Exception:
        return
    fm = dict(doc.frontmatter)
    canonical = str(fm.get("canonical_name") or "").strip()
    if not alias.strip() or alias.strip() == canonical:
        return
    aliases = list(fm.get("aliases") or [])
    if alias in aliases:
        return
    aliases.append(alias)
    fm["aliases"] = aliases
    fm["updated"] = today_iso()
    write_markdown(path, fm, doc.body)
