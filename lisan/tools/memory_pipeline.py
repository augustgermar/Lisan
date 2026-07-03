from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import AssemblerAgent, InterlocutorAgent, ListenerAgent, SkepticAgent, WriterAgent
from .heuristic_gate import is_correction_turn
from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .elicitor_session import run_elicitor_session
from .domain_fields import with_domain_fields
from .firewall import scan_text
from .log import log_error
from ..utils import listify
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
from .self_model import cached_capability_index
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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


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
    # conversation_id makes the cross-conversation preamble fire on the
    # extraction path as well as the elicitor path.
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
        "retrieved_context": context,
        "transcript": str(transcript_path.relative_to(vault)),
        "conversation_policy": json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    }
    record_inline_step("memory_pipeline.interlocutor")
    # Never forward skeptic flags to the interlocutor: skeptic uncertainty about a memory record bleeds into the user-facing
    # response ("this family member" instead of the named person). The
    # interlocutor speaks to the user; it must not see internal review notes.
    interlocutor_agent = InterlocutorAgent(vault=vault)
    interlocutor = interlocutor_agent.run_json(
        json.dumps(
            _interlocutor_input(
                writer=None,
                listener=listener,
                prior_state=prior_state,
                user_text=text,
                vault=vault,
                retrieved_context=context,
            ),
            indent=2,
            ensure_ascii=True,
        ),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
        capabilities=cached_capability_index(),
        db_path=db_path,
        conversation_id=conversation_id,
    )
    tool_calls = list(getattr(interlocutor_agent, "last_tool_calls", []) or [])
    record_inline_step("memory_pipeline.writer")
    # The episode path is split across two writer calls. The core call returns body + claims;
    # the artifact call returns entities / decisions / open loops / state /
    # evidence. Non-episode tasks stay single-shot — they're already small.
    writer_task = "episode_full_turn" if task == "episode" and tool_calls else ("episode_core" if task == "episode" else task)
    if task == "episode":
        writer_core = WriterAgent(vault=vault).run_json(
            text,
            task=writer_task,
            interlocutor_response=json.dumps(interlocutor, indent=2, ensure_ascii=True),
            tool_calls=json.dumps(tool_calls, indent=2, ensure_ascii=True),
            **common_kwargs,
        )
        writer = dict(writer_core)
    else:
        writer = WriterAgent(vault=vault).run_json(
            text,
            task=writer_task,
            interlocutor_response=json.dumps(interlocutor, indent=2, ensure_ascii=True),
            tool_calls=json.dumps(tool_calls, indent=2, ensure_ascii=True),
            **common_kwargs,
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
        db_path=db_path,
        conversation_id=conversation_id,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    skeptic_approved = _skeptic_approves(skeptic)
    # Only run the artifact pass when the skeptic approved the core.
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
            # resolved through evidence_id_map. Claims run before the
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
        tool_calls=getattr(interlocutor_agent, "last_tool_calls", []),
    )


def _merge_writer_outputs(core: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Combine the episode_core and episode_artifacts JSON payloads.

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

    # An explicit action request must reach the tool-bearing interlocutor;
    # the elicitor can only converse, so routing it there produces capability
    # dodges ("what should the ingestion produce?") instead of action.
    if action != "skip" and mode == "elicitor" and _is_action_request(ctx.text):
        mode = "extraction"
        applied_overrides.append("action_request_extraction")

    return RoutingDecision(
        listener=listener,
        action=action,
        mode=mode,
        applied_overrides=tuple(applied_overrides),
    )


_ACTION_VERB_RE = re.compile(
    r"\b(?:show|read|list|display|open|check(?:\s+out)?|ingest|absorb|import|scan|run|execute|install|fix|delete|move|copy|rename|schedule)\b"
)
_FS_PATH_RE = re.compile(r"(?:^|[\s'\"(])~?/[^\s'\"]+")
_FILE_OBJECT_RE = re.compile(r"\b(?:file|files|folder|directory|directories|path|ingest|ingestion|absorb|import)\b")
_FILE_ACTION_VERB_RE = re.compile(r"\b(?:ingest|absorb|import|scan|read|list|show|display|check(?:\s+out)?)\b")


def _is_action_request(text: str) -> bool:
    """Structural detection (no keyword lists over life content): an action
    verb aimed at a filesystem path, or a file/ingestion object paired with a
    file-action verb. Asymmetry is deliberate — a narrative turn misrouted to
    extraction still gets the normal capture pipeline, while an action request
    misrouted to the elicitor cannot act at all."""
    lowered = text.lower()
    if not _ACTION_VERB_RE.search(lowered):
        return False
    if _FS_PATH_RE.search(text):
        return True
    return bool(_FILE_OBJECT_RE.search(lowered) and _FILE_ACTION_VERB_RE.search(lowered))


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

    # Only route to the recall answerer when the turn is plausibly a
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

    # A recall turn must *answer* the question from the retrieved records,
    # not dump raw summaries. Route through the Interlocutor (it already speaks to
    # the user and renders deixis), strictly grounded in the records with an
    # explicit no-fabrication instruction. Render the records to the
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

    Reuses the InterlocutorAgent deliberately — one voice speaks to the user. It is the
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
    except Exception as exc:
        log_error(vault, "interlocutor response generation failed", exc)
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
    writer: dict[str, Any] | None,
    listener: dict[str, Any],
    prior_state: Any,
    user_text: str = "",
    vault: Path | None = None,
    retrieved_context: str | None = None,
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

    writer = writer or {}
    payload: dict[str, Any] = {
        # The current turn, verbatim (raw first-person user text, like
        # user_correction). The interlocutor runs before the writer since the
        # tool loop landed, so writer_summary is empty on the live path — and
        # an agent told to "respond to what the user just said" must actually
        # be shown what the user just said, not just retrieval echoes of it.
        "user_message": user_text,
        "writer_summary": _i(writer.get("summary") or ""),
        "writer_questions": writer.get("questions") or [],
        "memory_type": memory_type,
        "significance": writer.get("significance") or "medium",
        "entities": [e.get("name") for e in (writer.get("entities_to_create") or []) if isinstance(e, dict) and e.get("name")],
        "decisions": [_i(d.get("title")) for d in (writer.get("decisions_to_create") or []) if isinstance(d, dict) and d.get("title")],
        "open_loops": [_i(o.get("title")) for o in (writer.get("open_loops_to_create") or []) if isinstance(o, dict) and o.get("title")],
    }
    if retrieved_context:
        payload["retrieved_context"] = retrieved_context
    if is_correction:
        # On correction turns the prior narrative state predates the correction
        # and may actively contradict it — omit it to prevent the interlocutor
        # from echoing stale facts. Pass the raw user text instead so the
        # response can mirror the correction directly.
        payload["user_correction"] = user_text
        payload["narrative_state"] = {}
    else:
        emotional_texture = _current_emotional_texture(listener, prior_state)
        payload["narrative_state"] = {
            "story_thread": _i(getattr(prior_state, "story_thread", "") or ""),
            "established": [_i(x) for x in (getattr(prior_state, "established", []) or [])],
            "open_threads": [_i(x) for x in (getattr(prior_state, "open_threads", []) or [])],
            "emotional_texture": _i(emotional_texture),
            "turn_count": getattr(prior_state, "turn_count", 0),
        }
    return payload


def _current_emotional_texture(listener: dict[str, Any], prior_state: Any) -> str:
    prior_texture = str(getattr(prior_state, "emotional_texture", "") or "").strip()
    if not prior_texture:
        return ""
    if _listener_has_affect_signal(listener):
        return prior_texture
    return ""


def _listener_has_affect_signal(listener: dict[str, Any]) -> bool:
    score = listener.get("score", 0)
    try:
        if int(score) >= 4:
            return True
    except (TypeError, ValueError):
        pass
    reasons = [str(reason).lower() for reason in (listener.get("reason") or [])]
    return any("affect" in reason or "emotion" in reason or "distress" in reason for reason in reasons)


def _has_distress_signal(listener: dict[str, Any], text: str) -> bool:
    reasons = [str(r).lower() for r in (listener.get("reason") or [])]
    if any("affect" in r or "high-risk" in r or "biograph" in r for r in reasons):
        return True
    lowered = text.lower()
    distress_phrases = (
        "i don't know what to do", "i'm worried", "i'm scared", "i'm anxious",
        "freaking out", "falling apart", "can't handle", "feel awful",
        "hurts", "miss", "lonely", "overwhelmed", "stressed", "exhausted",
        # distress can be third-person too (talking about a loved one)
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
            except Exception as exc:
                log_error(vault, f"supersede corrected record failed for {rid}", exc)


def _conversation_turn_count(vault: Path, conversation_id: str | None) -> int:
    """Count completed USER turns for `conversation_id` from today's transcript.

    Narrative state only increments on the elicitor path, so
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
    # The filename hashes the source text rather than using a live timestamp,
    # making it deterministic per turn: retries overwrite the earlier draft
    # instead of accumulating siblings. The today_iso() prefix preserves
    # uniqueness across days for the rare case of identical text on two days.
    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    slug = slugify(str(writer.get("summary") or text[:48]))[:80]
    path = vault / "drafts" / f"{today_iso()}-{content_hash}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rejected drafts are held for Dreamer review with a distinct
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
        # Enforce word/sentence-boundary truncation on the
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

from .entity_resolution import (
    _create_entity_stubs,
    _create_relationship_edges,
    _normalize_entity_subtype,
    _looks_like_organization,
    _has_person_role_context,
    _looks_like_entity,
    _load_entity_index,
    _entity_resolution_candidates,
    _entity_disambiguator,
    _pascalize_token,
    _entity_first_token,
    _entity_name_roots,
    _scan_user_stated_handle,
    _entity_nickname,
    _entity_identity_names,
    _same_first_name_records,
    _assign_entity_nickname,
    _ensure_nicknames_for_collision,
    _entity_disambiguator_from_candidates,
    _match_existing_entity,
    _entity_subtype,
    _entity_name_tokens,
    _candidate_has_surname_conflict,
    _append_entity_alias,
)
