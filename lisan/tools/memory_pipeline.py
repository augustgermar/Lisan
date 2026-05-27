from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import AssemblerAgent, InterlocutorAgent, ListenerAgent, SkepticAgent, WriterAgent
from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .elicitor_session import run_elicitor_session
from .domain_fields import with_domain_fields
from .firewall import scan_text
from .log import log_error
from .epistemic import listify
from .narrative_state import load_narrative_state
from .record_fanout import (
    register_claim_reference,
    register_evidence_reference,
    resolve_claim_links,
    resolve_evidence_links,
)
from .tracing import record_inline_step
from .record_factory import (
    STATE_TTLS,
    new_claim,
    new_decision,
    new_evidence,
    new_entity,
    new_open_loop,
    normalize_state_category,
    upsert_state,
)
from .transcripts import append_transcript
from ..agents.writer import _truncate_summary as _truncate_summary_boundary


# Domains that may be inferred from a record's content alone.
_RELATIONAL_TERMS = (
    "mom", "dad", "mother", "father", "wife", "husband", "spouse",
    "partner", "sister", "brother", "daughter", "son", "kid", "kids",
    "child", "children", "family", "parent", "co-parent", "ex",
)
_WORK_TERMS = (
    "manager", "boss", "team", "co-worker", "coworker", "colleague",
    "engineering", "sprint", "standup", "review", "project", "meeting",
    "ticket", "deploy", "release",
)
_PRONOUN_RE = re.compile(r"\b(she|he|they|her|him|them)\b", re.IGNORECASE)


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
    action = str(listener.get("action", "skip"))
    mode = str(listener.get("mode", "skip"))

    # Never fully skip a conversational turn — the heuristic governs capture, not response.
    # Upgrade to lightweight elicitor when:
    #   - mid-conversation (turn_count > 0), OR
    #   - message has seed potential (seed_score > 0, e.g. "oh man what a day!")
    # Exception: topic explicitly closed.
    seed_score = int(listener.get("seed_score", 0))
    if (
        action == "skip"
        and prior_state.mode_status not in ("closed",)
        and (prior_state.turn_count > 0 or seed_score > 0)
    ):
        action = "lightweight"
        mode = "elicitor"

    # Finding 9: Turn-1 elicitor preference.
    # An opening emotional turn deserves to be heard before it is processed.
    # We compute turn position from the transcript (v0.1.7) — narrative state
    # only ticks on the elicitor path, so extraction-only conversations would
    # otherwise stay at turn_count=0 forever and the preference would mis-fire
    # on later turns. The transcript already includes this turn's appended
    # line, so the very first turn comes back as 1 and the preference is
    # gated to "first turn only".
    transcript_turn_index = _conversation_turn_count(vault, conversation_id)
    if (
        action != "skip"
        and mode == "extraction"
        and transcript_turn_index <= 1
        and _has_distress_signal(listener, text)
    ):
        mode = "elicitor"

    if action == "skip":
        return MemoryPipelineResult(
            transcript_path=transcript_path,
            draft_path=None,
            listener=listener,
            writer=None,
            skeptic=None,
            interlocutor=None,
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
            _interlocutor_input(writer=writer_core, listener=listener, prior_state=prior_state),
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
    draft_path = _write_draft(
        vault, text, transcript_path, listener, writer, skeptic, interlocutor,
        task, mode, action, skeptic_approved,
    )
    record_inline_step("memory_pipeline.fanout")
    draft_rel = str(draft_path.relative_to(vault))
    # Entity stubs, decisions, and open loops are exempt from the skeptic gate
    # — they don't carry the same inference risk as state updates, evidence,
    # and claims (which encode the writer's interpretation as durable truth).
    _create_entity_stubs(vault, writer, draft_rel, text)
    _create_open_loops(vault, writer, draft_rel)
    _create_decisions(vault, writer, draft_rel)
    if skeptic_approved:
        # Evidence runs before claims so claim.supporting_evidence can be
        # resolved through evidence_id_map (Finding 4). Claims run before the
        # state update so the state can reference resolved claim IDs in future
        # passes.
        evidence_id_map = _create_evidence_records(
            vault, writer, transcript_path, draft_rel,
        )
        claim_id_map = _create_claim_records(
            vault, writer, draft_rel,
            db_path=db_path,
            evidence_id_map=evidence_id_map,
        )
        _apply_state_updates(vault, writer, draft_rel)
    else:
        record_inline_step("memory_pipeline.fanout.skeptic_blocked")
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


def _choose_task(text: str, listener: dict[str, Any]) -> str:
    # Primary: use the LLM's explicit memory type classification
    memory_type = str(listener.get("memory_type") or "").lower()
    if memory_type in ("decision", "open_loop", "state", "knowledge", "entity"):
        return memory_type
    return "episode"


def _skeptic_approves(skeptic: dict[str, Any] | None) -> bool:
    """Finding 2: gate state/evidence/claim fanout on skeptic approval."""
    if not isinstance(skeptic, dict):
        return True
    approved = skeptic.get("approved")
    if approved is False:
        return False
    action = str(skeptic.get("recommended_action") or "").lower()
    if action in {"revise", "hold", "needs_revision"}:
        return False
    return True


def _interlocutor_input(
    writer: dict[str, Any],
    listener: dict[str, Any],
    prior_state: Any,
) -> dict[str, Any]:
    """Build a clean conversational payload — no skeptic notes, no internal flags."""
    return {
        "writer_summary": writer.get("summary") or "",
        "writer_questions": writer.get("questions") or [],
        "memory_type": listener.get("memory_type") or "",
        "significance": writer.get("significance") or "medium",
        "entities": [e.get("name") for e in (writer.get("entities_to_create") or []) if isinstance(e, dict) and e.get("name")],
        "decisions": [d.get("title") for d in (writer.get("decisions_to_create") or []) if isinstance(d, dict) and d.get("title")],
        "open_loops": [o.get("title") for o in (writer.get("open_loops_to_create") or []) if isinstance(o, dict) and o.get("title")],
        "narrative_state": {
            "story_thread": getattr(prior_state, "story_thread", "") or "",
            "established": list(getattr(prior_state, "established", []) or []),
            "open_threads": list(getattr(prior_state, "open_threads", []) or []),
            "emotional_texture": getattr(prior_state, "emotional_texture", "") or "",
            "turn_count": getattr(prior_state, "turn_count", 0),
        },
    }


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


# ── Helpers shared by all fanout functions ────────────────────────────────────

def _entity_canonical_names(writer: dict[str, Any]) -> list[str]:
    """Pull canonical entity names from the writer's entities_to_create."""
    names: list[str] = []
    for entry in writer.get("entities_to_create") or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _resolve_pronouns(summary: str, entity_names: list[str]) -> str:
    """Finding 8: resolve a leading pronoun against the writer's entity list.

    When a summary starts with "She/He/They …" and exactly one entity is on
    record, substitute the canonical name. We don't try to resolve mid-sentence
    pronouns — the heuristic stays conservative on purpose; ambiguous cases stay
    untouched so they surface in skeptic review instead of getting silently
    rewritten.
    """
    if not summary or not entity_names:
        return summary
    # Pick a candidate: exactly one named person makes substitution safe.
    if len(entity_names) != 1:
        return summary
    name = entity_names[0]

    def repl(match: re.Match[str]) -> str:
        pronoun = match.group(0)
        # Only rewrite subject/object pronouns at sentence-leading positions;
        # other positions keep the pronoun (it's the right call grammatically).
        start = match.start()
        prefix = summary[:start].rstrip()
        if prefix and not prefix.endswith((".", "!", "?")):
            return pronoun
        # Preserve case of the pronoun's first letter.
        return name if pronoun[0].isupper() else name.lower()

    return _PRONOUN_RE.sub(repl, summary)


def _infer_domain(
    explicit: str | None,
    fallback: str,
    *,
    text: str | None = None,
    entity_names: list[str] | None = None,
) -> str:
    """Finding 7: tighten domain assignment.

    Order of preference:
      1. Explicit, valid value from the writer.
      2. Hint from the entity list and summary text (named relatives / work terms).
      3. The caller's fallback.
    """
    valid = set(STATE_TTLS.keys()) | {"cross_arena"}
    explicit_clean = (explicit or "").strip().lower()
    if explicit_clean in valid and explicit_clean != "cross_arena":
        return explicit_clean
    haystack_parts: list[str] = []
    if text:
        haystack_parts.append(text)
    if entity_names:
        haystack_parts.append(" ".join(entity_names))
    haystack = " ".join(haystack_parts).lower()
    if haystack:
        if any(term in haystack for term in _RELATIONAL_TERMS):
            return "relational"
        if any(term in haystack for term in _WORK_TERMS):
            return "work"
    if explicit_clean in valid:
        return explicit_clean
    return fallback


def _merge_links(*sources: Any) -> list[str]:
    """Flatten / dedupe link sources for a record's `links` frontmatter field."""
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        for item in listify(src):
            value = str(item).strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def _basis_or_default(entry: Any, default: str) -> str:
    """Finding 6: pull per-record confidence_basis from the writer; fall back only when missing."""
    if isinstance(entry, dict):
        explicit = str(entry.get("confidence_basis") or "").strip()
        if explicit:
            return explicit
    return default


# ── Fanout: open loops ────────────────────────────────────────────────────────

def _create_open_loops(vault: Path, writer: dict[str, Any], draft_rel: str) -> None:
    """Materialize open loops immediately — open loops are always capture_now per spec.

    Finding 3 (v0.1.7): skip any loop whose `owner` is not the user. The
    writer prompt now asks for explicit ownership, and we filter as a backstop
    so other people's pending questions (for example, a family member
    wondering whether to share an update; a parent's medication concern)
    never become user-owned todos.
    """
    loops = writer.get("open_loops_to_create") or []
    entity_names = _entity_canonical_names(writer)
    for loop in loops:
        if not isinstance(loop, dict):
            continue
        title = str(loop.get("title") or "").strip()
        next_action = str(loop.get("next_action") or "").strip()
        summary = str(loop.get("summary") or "").strip()
        priority = str(loop.get("priority") or "medium").strip()
        owner = str(loop.get("owner") or "user").strip().lower()
        explicit_domain = str(loop.get("domain", loop.get("arena")) or "").strip()
        domain = _infer_domain(
            explicit_domain,
            fallback="cross_arena",
            text=f"{title} {summary} {next_action}",
            entity_names=entity_names,
        )
        if not title or not next_action:
            continue
        if owner not in {"user", "self", "me", ""}:
            # Other people's pending actions aren't user loops.
            continue
        if priority not in ("low", "medium", "high"):
            priority = "medium"
        try:
            new_open_loop(
                vault=vault,
                title=title,
                domain_primary=domain,
                summary=summary or title,
                next_action=next_action,
                priority=priority,
                confidence="low",
                confidence_basis=_basis_or_default(loop, "Auto-extracted from conversation"),
                links=_merge_links(loop.get("linked_claims"), loop.get("linked_episodes"), [draft_rel]),
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.open_loop", exc)


# ── Fanout: state ─────────────────────────────────────────────────────────────

def _apply_state_updates(vault: Path, writer: dict[str, Any], draft_rel: str) -> None:
    updates = writer.get("state_updates") or []
    entity_names = _entity_canonical_names(writer)
    for update in updates:
        if not isinstance(update, dict):
            continue
        raw_category = update.get("category", update.get("arena"))
        summary = str(update.get("summary") or "").strip()
        confidence = str(update.get("confidence") or "low").strip()
        # Finding 7: prefer relational when a named primer entity is referenced.
        inferred = _infer_domain(
            str(raw_category or ""),
            fallback="",
            text=summary,
            entity_names=entity_names,
        )
        state_category = normalize_state_category(inferred or raw_category, summary=summary)
        if not state_category or not summary or state_category not in STATE_TTLS:
            continue
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        # Finding 8: never write a pronoun-led state summary to a persistent file.
        summary = _resolve_pronouns(summary, entity_names)
        try:
            upsert_state(
                vault=vault,
                state_category=state_category,
                summary=summary,
                confidence=confidence,
                confidence_basis=_basis_or_default(update, "Auto-extracted from conversation"),
                sources=_merge_links(update.get("sources"), [draft_rel]),
            )
        except Exception as exc:
            log_error(vault, "memory_pipeline.state_update", exc)


# ── Fanout: entities ──────────────────────────────────────────────────────────

def _create_entity_stubs(vault: Path, writer: dict[str, Any], draft_rel: str, source_text: str) -> None:
    """Materialize entity stubs proposed by the writer.

    Finding 1 (v0.1.7): dedupe across short/full name variants.
    Finding #4 (v26.5.27): reject nonsense entity proposals (days of the week,
    adverbs, tool names, single capitalized words that are not in the primer
    cast).
    Finding #5 (v26.5.27): refuse to merge two multi-word entities just
    because they share a surname token — require >= 2 shared tokens, or a
    full-name match, or a primer-cast tiebreaker.
    """
    from .primer_index import known_names as _primer_known_names

    entities = writer.get("entities_to_create") or []
    if not entities:
        return
    index = _load_entity_index(vault)
    primer_cast = _primer_known_names(vault)
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

        subtype = _normalize_entity_subtype(
            name=name,
            subtype=str(entry.get("subtype") or "person").strip(),
            summary=summary,
            source_text=source_text,
            primer_cast=primer_cast,
        )
        if not subtype:
            continue

        # Finding #4: validate before any creation or merge.
        if not _looks_like_entity(name, subtype, primer_cast):
            continue

        existing = _match_existing_entity(name, subtype, index, primer_cast)
        if existing is not None:
            _append_entity_alias(existing, name)
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
                confidence_basis=_basis_or_default(entry, "Auto-extracted from conversation"),
            )
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


def _looks_like_entity(name: str, subtype: str, primer_cast: frozenset[str]) -> bool:
    """Finding #4: validate that *name* is plausibly an entity of *subtype*.

    Rules:
    - Primer-known names are *always* accepted, regardless of subtype-specific
      shape rules. This is what lets users with month-or-day names ("August",
      "May", "Friday" as a child) be recognized once they're in identity.md.
    - For ``subtype == "person"``: require at least two capitalized tokens,
      *or* exact membership in the primer cast. A bare capitalized word
      ("Slack", "Strategically", "What") is rejected unless the primer
      whitelists it.
    - For other subtypes (place, thing, organization, project): always accept
      — those are more permissive in shape and rarely produce nonsense.
    """
    from .stopwords import SENTENCE_INITIAL_OR_TOOL_STOPWORDS, MONTH_STOPWORDS, DAY_STOPWORDS

    if not name:
        return False

    # Primer allowlist wins over every blocklist. The user's own name "August"
    # is in the primer, so even though "August" is in MONTH_STOPWORDS, this
    # branch returns True before we ever consult the blocklist.
    if name in primer_cast:
        return True

    # Names like "Mr X" or full names with proper capitalization are still
    # checked against shape rules below.
    tokens = name.split()
    if not tokens:
        return False

    if subtype == "person":
        # Single-word person names need primer support (handled above).
        if len(tokens) < 2:
            return False
        # Reject if any token looks like a stopword (days, tools, adverbs).
        # Months are intentionally excluded from SENTENCE_INITIAL_OR_TOOL_STOPWORDS;
        # we check them separately so primer-cast members survive.
        for tok in tokens:
            if tok in SENTENCE_INITIAL_OR_TOOL_STOPWORDS:
                return False
            if tok in DAY_STOPWORDS:
                return False
            # Months only block if they're not in the primer (already short-
            # circuited above).
            if tok in MONTH_STOPWORDS and tok not in primer_cast:
                return False
        # All tokens must look like proper-noun shape.
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


# ── Fanout: evidence ──────────────────────────────────────────────────────────

def _create_evidence_records(
    vault: Path,
    writer: dict[str, Any],
    transcript_path: Path,
    draft_rel: str,
) -> dict[str, str]:
    """Create evidence records and return a title→evidence_id map.

    Evidence is materialized BEFORE claims in the v0.1.7 fanout order so the
    claim-creation step can resolve `supporting_evidence` strings that the
    writer expressed as evidence titles (Finding 4).
    """
    evidence_items = writer.get("evidence_to_create") or []
    transcript_rel = str(transcript_path.relative_to(vault))
    entity_names = _entity_canonical_names(writer)
    evidence_id_map: dict[str, str] = {}
    for entry in evidence_items:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("summary") or "").strip()
        if not title:
            continue
        arena = _infer_domain(
            str(entry.get("arena") or ""),
            fallback="cross_arena",
            text=f"{title} {entry.get('summary') or ''}",
            entity_names=entity_names,
        )
        try:
            created = new_evidence(
                vault=vault,
                title=title,
                source_type=str(entry.get("source_type") or "manual_note").strip(),
                source_uri=str(entry.get("source_uri") or transcript_rel),
                artifact_ref=str(entry.get("artifact_ref") or transcript_rel),
                artifact_hash=str(entry.get("artifact_hash") or "").strip() or None,
                timestamp_of_artifact=str(entry.get("timestamp_of_artifact") or "").strip() or None,
                actors=listify(entry.get("actors")),
                arena=arena,
                compartments=listify(entry.get("compartments")),
                sensitivity=str(entry.get("sensitivity") or "low").strip(),
                reliability=str(entry.get("reliability") or "medium").strip(),
                summary=str(entry.get("summary") or title),
                observed_facts=listify(entry.get("observed_facts")),
                verbatim_excerpt=str(entry.get("verbatim_excerpt") or "").strip() or None,
                # Claims haven't been created yet at this point; we record the
                # raw writer-supplied link strings and let the rebuild-index
                # pass resolve them later. Episodes always include the draft.
                linked_claims=listify(entry.get("linked_claims")),
                linked_episodes=_merge_links(entry.get("linked_episodes"), [draft_rel]),
                confidence_basis=_basis_or_default(entry, "Auto-extracted from conversation"),
            )
            evidence_doc = load_markdown(created.path)
            evidence_id = str(evidence_doc.frontmatter.get("id") or "")
            if evidence_id:
                register_evidence_reference(evidence_id_map, entry, evidence_id)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.evidence", exc)
    return evidence_id_map


# ── Fanout: claims ────────────────────────────────────────────────────────────

def _create_claim_records(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    db_path: Path | None = None,
    evidence_id_map: dict[str, str] | None = None,
) -> dict[str, str]:
    claims = writer.get("claims_to_create") or []
    claim_id_map: dict[str, str] = {}
    entity_names = _entity_canonical_names(writer)
    for entry in claims:
        if not isinstance(entry, dict):
            continue
        claim_text = str(entry.get("claim_text") or entry.get("summary") or "").strip()
        if not claim_text:
            continue
        claim_confidence = entry.get("confidence", 0.5)
        try:
            confidence = float(claim_confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        arena = _infer_domain(
            str(entry.get("arena") or ""),
            fallback="cross_arena",
            text=claim_text,
            entity_names=entity_names,
        )
        # Finding 4: rewrite writer-supplied evidence titles into evidence IDs
        # so the claim links resolve under validation.
        supporting = resolve_evidence_links(
            listify(entry.get("supporting_evidence")), evidence_id_map or {},
        ) or listify(entry.get("supporting_evidence"))
        contradicting = resolve_evidence_links(
            listify(entry.get("contradicting_evidence")), evidence_id_map or {},
        ) or listify(entry.get("contradicting_evidence"))
        try:
            created = new_claim(
                vault=vault,
                claim_text=claim_text,
                claim_class=str(entry.get("claim_class") or "interpretation").strip(),
                owner=str(entry.get("owner") or "user").strip(),
                status=str(entry.get("status") or "active").strip(),
                confidence=confidence,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                linked_patterns=listify(entry.get("linked_patterns")),
                first_seen=str(entry.get("first_seen") or "").strip() or None,
                last_reviewed=str(entry.get("last_reviewed") or "").strip() or None,
                review_notes=str(entry.get("review_notes") or "").strip(),
                arena=arena,
                compartments=list(entry.get("compartments") or []),
                privacy=str(entry.get("privacy") or "personal").strip(),
                significance=str(entry.get("significance") or "low").strip(),
                summary=str(entry.get("summary") or claim_text[:120]),
                confidence_basis=_basis_or_default(
                    entry, "Claim confidence assessed from supporting and contradicting evidence",
                ),
            )
            claim_doc = load_markdown(created.path)
            claim_id = str(claim_doc.frontmatter.get("id") or "")
            if claim_id:
                register_claim_reference(claim_id_map, entry, claim_id)
                # Finding 3: persist the claim into the SQLite claims table so
                # retrieval, contradiction detection, and Dreamer can actually
                # see it. The markdown file is the source of truth; the SQLite
                # row is the queryable projection.
                _index_claim_row(
                    vault=vault,
                    claim_id=claim_id,
                    draft_rel=draft_rel,
                    entry=entry,
                    confidence=confidence,
                    claim_text=claim_text,
                    db_path=db_path,
                )
                _link_claim_to_draft(vault=vault, claim_id=claim_id, draft_rel=draft_rel)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.claim", exc)
    return claim_id_map


def _index_claim_row(
    *,
    vault: Path,
    claim_id: str,
    draft_rel: str,
    entry: dict[str, Any],
    confidence: float,
    claim_text: str,
    db_path: Path | None = None,
) -> None:
    """Write the new claim into the SQLite claims table (Finding 3)."""
    try:
        import sqlite3

        from ..paths import sqlite_path

        db_path = db_path or sqlite_path()
        if not db_path.exists():
            return
        conn = sqlite3.connect(db_path)
        try:
            today = today_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO claims (
                    id, episode_id, claim_text, claim_type, confidence, sensitivity,
                    source_basis, evidence_id, status, created, last_reviewed, review_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    draft_rel,
                    claim_text,
                    str(entry.get("claim_class") or "interpretation"),
                    f"{confidence:.3f}",
                    str(entry.get("sensitivity") or "low"),
                    str(entry.get("review_notes") or "Auto-extracted from conversation"),
                    ", ".join(listify(entry.get("supporting_evidence"))),
                    str(entry.get("status") or "active"),
                    today,
                    today,
                    today,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log_error(vault, "memory_pipeline.claim.index", exc)


def _link_claim_to_draft(*, vault: Path, claim_id: str, draft_rel: str) -> None:
    """Finding 5: ensure the claim's `links` frontmatter records the draft origin."""
    if not draft_rel:
        return
    try:
        # Locate the claim file by id.
        for path in (vault / "claims").glob("*.md"):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            if str(doc.frontmatter.get("id") or "") != claim_id:
                continue
            fm = dict(doc.frontmatter)
            fm["links"] = _merge_links(fm.get("links"), [draft_rel])
            write_markdown(path, fm, doc.body)
            return
    except Exception as exc:
        log_error(vault, "memory_pipeline.claim.link", exc)


# ── Fanout: decisions ─────────────────────────────────────────────────────────

def _create_decisions(vault: Path, writer: dict[str, Any], draft_rel: str | None = None) -> None:
    decisions = list(writer.get("decisions_to_create") or [])
    entity_names = _entity_canonical_names(writer)
    if not decisions and str(writer.get("record_type") or "").strip().lower() == "decision":
        summary = str(writer.get("summary") or "").strip()
        if summary:
            _raw_fm = writer.get("frontmatter")
            frontmatter: dict[str, Any] = _raw_fm if isinstance(_raw_fm, dict) else {}
            decisions = [{
                "title": summary,
                "summary": summary,
                "domain": frontmatter.get("domain_primary") or frontmatter.get("domain") or "cross_arena",
                "significance": writer.get("significance") or "medium",
                "alternatives_considered": listify(frontmatter.get("alternatives_considered")),
                "revisit_conditions": listify(frontmatter.get("revisit_conditions")),
                "confidence_basis": frontmatter.get("confidence_basis"),
                "linked_episodes": listify(frontmatter.get("linked_episodes")),
                "linked_claims": listify(frontmatter.get("linked_claims")),
            }]
    for entry in decisions:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        domain = _infer_domain(
            str(entry.get("domain", entry.get("arena")) or ""),
            fallback="cross_arena",
            text=f"{title} {summary}",
            entity_names=entity_names,
        )
        significance = str(entry.get("significance") or "low").strip()
        alternatives = listify(entry.get("alternatives_considered"))
        revisit = listify(entry.get("revisit_conditions"))
        if not title or not summary:
            continue
        if significance not in ("low", "medium", "high"):
            significance = "low"
        try:
            new_decision(
                vault=vault,
                title=title,
                domain_primary=domain,
                summary=summary,
                significance=significance,
                confidence="low",
                confidence_basis=_basis_or_default(entry, "Auto-extracted from conversation"),
                alternatives_considered=alternatives,
                revisit_conditions=revisit,
                links=_merge_links(
                    entry.get("linked_claims"),
                    entry.get("linked_episodes"),
                    [draft_rel] if draft_rel else [],
                ),
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.decision", exc)
