from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
from .deixis import render_deixis, tokenize_principal, tokenize_principal_obj
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
        _create_entity_stubs(vault, writer, draft_rel, text, frequent_names=frequent_names, index_conn=index_conn)
        _create_relationship_edges(vault, writer, db_path=db_path, index_conn=index_conn)
        fanout_open_loops(vault, writer, draft_rel, index_conn=index_conn)
        fanout_decisions(vault, writer, draft_rel, index_conn=index_conn)
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

    lines = ["Here's what I found in your stored records:"]
    for item in items[:3]:
        summary = str(item.summary or "").strip()
        if not summary:
            summary = item.id
        lines.append(f"- {summary}")
    return "\n".join(lines)


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
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
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
) -> None:
    """Materialize entity stubs proposed by the writer."""
    from .primer_index import known_names as _primer_known_names
    from .primer_index import roster as _roster
    from .entity_kind import assign_kind

    entities = writer.get("entities_to_create") or []
    if not entities:
        return
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
    for entry in entities:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not name:
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

        if not _looks_like_entity(name, subtype, allowlist, source_text):
            continue

        existing = _match_existing_entity(name, subtype, index, allowlist)
        if existing is not None:
            _append_entity_alias(existing, name)
            index_created_record(vault, CreatedRecord(path=existing, created=True), index_conn)
            # Refresh the in-memory index so the next sibling in the same pass
            # also resolves to this canonical entity. Full-name key only —
            # surname tokens stay subject to the strict-token rule.
            index.setdefault(name.lower(),
                             {"path": existing, "kind": "full", "canonical": name})
            continue
        try:
            created = new_entity(
                vault=vault,
                name=name,
                subtype=subtype,
                summary=summary or f"{name} mentioned in conversation.",
                confidence="low",
                confidence_basis=basis_or_default(entry, "Auto-extracted from conversation"),
            )
            index_created_record(vault, created, index_conn)
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

# Relationship/role words that, when found near a first name, confirm the
# name refers to a real person even though it is a single token.
_RELATIONSHIP_WORDS: frozenset[str] = frozenset({
    # Family
    "son", "daughter", "dad", "mom", "mother", "father", "brother", "sister",
    "husband", "wife", "partner", "uncle", "aunt", "grandpa", "grandma",
    "grandfather", "grandmother", "grandson", "granddaughter", "nephew",
    "niece", "cousin", "stepmom", "stepdad", "stepson", "stepdaughter",
    "fiance", "fiancee", "ex",
    # Professional / social
    "colleague", "coworker", "boss", "manager", "supervisor", "therapist",
    "lawyer", "attorney", "accountant", "mentor", "coach", "advisor",
    "friend", "neighbor", "roommate", "classmate", "teammate",
    "boyfriend", "girlfriend", "doctor",
})


def _has_person_role_context(name: str, source_text: str) -> bool:
    """Return True when the source text places *name* in a clear person-role context.

    Detects two patterns:
      - possessive-role-name: "my/his/her/their [role] [Name]" within a 4-word window
      - name-role appositive:  "[Name], my/his/her [role]" or "[Name] is my [role]"
    """
    if not source_text:
        return False
    lowered = source_text.lower()
    name_lower = name.lower()
    role_group = "(?:" + "|".join(re.escape(w) for w in _RELATIONSHIP_WORDS) + ")"
    possessive = r"(?:my|his|her|their|our)\s+(?:\w+\s+)?" + role_group + r"\s+" + re.escape(name_lower)
    appositive = re.escape(name_lower) + r"(?:,?\s+(?:my|his|her|their)\s+" + role_group + r"|\s+is\s+(?:my|his|her|their)\s+" + role_group + r")"
    return bool(re.search(possessive, lowered) or re.search(appositive, lowered))


def _looks_like_entity(name: str, subtype: str, primer_cast: frozenset[str], source_text: str = "") -> bool:
    """Validate that *name* is plausibly an entity of *subtype*.

    Rules (in priority order):
    - Primer-known names always accepted.
    - Title-prefixed names ("Dr. Kwan", "Ms. Reyes") always accepted as persons.
    - Single-token person names accepted when source text has role/relationship context.
    - Multi-token person names accepted when all tokens are proper-noun shaped
      and none are stopwords.
    - Non-person subtypes: light-touch validation only.
    """
    from .stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS, MONTH_STOPWORDS, DAY_STOPWORDS

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
            # Single first-name: accept only when role context is present in the
            # source text ("my son Theo", "my colleague Marcus").
            return _has_person_role_context(name, source_text)

        # Multi-token names: reject stopwords, require proper-noun shape.
        for tok in tokens:
            if tok in SENTENCE_INITIAL_OR_TOOL_STOPWORDS:
                return False
            if tok in DAY_STOPWORDS:
                return False
            if tok in MONTH_STOPWORDS and tok not in primer_cast:
                return False
        if not all(t[:1].isupper() and len(t) > 1 for t in tokens):
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
        aliases = doc.frontmatter.get("aliases") or []
        names = [canonical] + [str(a) for a in aliases if isinstance(a, str)]
        for name in names:
            if not name:
                continue
            key = name.lower()
            index.setdefault(key, {"path": path, "kind": "full", "canonical": canonical or name})
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


def _match_existing_entity(
    name: str,
    subtype: str,
    index: dict[str, dict[str, Any]],
    primer_cast: frozenset[str] | None = None,
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

    tokens = [t.lower() for t in name.split() if t]
    if not tokens:
        return None

    if len(tokens) == 1:
        # Single-word proposal can absorb into an existing multi-word entity
        # only when exactly one entity claims that token.
        entry = index.get(tokens[0])
        if entry is None:
            return None
        if entry.get("kind") in ("token", "full") and _entity_subtype(entry["path"]) == subtype:
            return entry["path"]
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
