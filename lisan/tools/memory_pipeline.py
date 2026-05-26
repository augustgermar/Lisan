from __future__ import annotations

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
from .record_fanout import register_claim_reference, resolve_claim_links
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
    # If we're at the very start of a conversation and the message carries
    # affect/distress signals, prefer elicitor over extraction even when the
    # listener rated it "full". Memory still gets formed on later turns or via
    # background jobs; the user gets a warm response immediately.
    if (
        action != "skip"
        and mode == "extraction"
        and prior_state.turn_count <= 1
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
    context = AssemblerAgent(vault=vault).run(text).text
    task = _choose_task(text=text, listener=listener)
    record_inline_step("memory_pipeline.writer")
    writer = WriterAgent(vault=vault).run_json(
        text,
        significance="high" if action == "full" else "medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        task=task,
        context=context,
        transcript=str(transcript_path.relative_to(vault)),
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    record_inline_step("memory_pipeline.skeptic")
    skeptic = SkepticAgent(vault=vault).run_json(
        json.dumps(writer, indent=2, ensure_ascii=True),
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
            _interlocutor_input(writer=writer, listener=listener, prior_state=prior_state),
            indent=2,
            ensure_ascii=True,
        ),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    draft_path = _write_draft(
        vault, text, transcript_path, listener, writer, skeptic, interlocutor,
        task, mode, action, skeptic_approved,
    )
    record_inline_step("memory_pipeline.fanout")
    draft_rel = str(draft_path.relative_to(vault))
    # Entity stubs, decisions, and open loops are exempt from the skeptic gate
    # — they don't carry the same inference risk as state updates, evidence,
    # and claims (which encode the writer's interpretation as durable truth).
    _create_entity_stubs(vault, writer, draft_rel)
    _create_open_loops(vault, writer, draft_rel)
    _create_decisions(vault, writer, draft_rel)
    if skeptic_approved:
        claim_id_map = _create_claim_records(vault, writer, draft_rel, db_path=db_path)
        _create_evidence_records(vault, writer, transcript_path, draft_rel, claim_id_map)
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
    distress_terms = (
        "i don't know what to do", "i'm worried", "i'm scared", "i'm anxious",
        "freaking out", "falling apart", "can't handle", "feel awful",
        "hurts", "miss", "lonely", "overwhelmed", "stressed", "exhausted",
    )
    return any(term in lowered for term in distress_terms)


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
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    slug = slugify(str(writer.get("summary") or text[:48]))[:80]
    path = vault / "drafts" / f"{today_iso()}-{timestamp}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Finding 2: rejected drafts are held for Dreamer review with a distinct
    # status so the batch review process can find them.
    draft_status = "pending" if skeptic_approved else "needs_revision"
    writer_fm = writer.get("frontmatter") or {}
    frontmatter = {
        "id": f"draft.memory.{timestamp}.{slug}",
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
        "summary": str(writer.get("summary") or text[:120]),
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
    """Materialize open loops immediately — open loops are always capture_now per spec."""
    loops = writer.get("open_loops_to_create") or []
    entity_names = _entity_canonical_names(writer)
    for loop in loops:
        title = str(loop.get("title") or "").strip()
        next_action = str(loop.get("next_action") or "").strip()
        summary = str(loop.get("summary") or "").strip()
        priority = str(loop.get("priority") or "medium").strip()
        explicit_domain = str(loop.get("domain", loop.get("arena")) or "").strip()
        domain = _infer_domain(
            explicit_domain,
            fallback="cross_arena",
            text=f"{title} {summary} {next_action}",
            entity_names=entity_names,
        )
        if not title or not next_action:
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

def _create_entity_stubs(vault: Path, writer: dict[str, Any], draft_rel: str) -> None:
    entities = writer.get("entities_to_create") or []
    for entry in entities:
        name = str(entry.get("name") or "").strip()
        subtype = str(entry.get("subtype") or "person").strip()
        summary = str(entry.get("summary") or "").strip()
        if not name:
            continue
        try:
            new_entity(
                vault=vault,
                name=name,
                subtype=subtype,
                summary=summary or f"{name} mentioned in conversation.",
                confidence="low",
                confidence_basis=_basis_or_default(entry, "Auto-extracted from conversation"),
            )
        except FileExistsError:
            pass  # entity already exists — skip silently


# ── Fanout: evidence ──────────────────────────────────────────────────────────

def _create_evidence_records(
    vault: Path,
    writer: dict[str, Any],
    transcript_path: Path,
    draft_rel: str,
    claim_id_map: dict[str, str] | None = None,
) -> None:
    evidence_items = writer.get("evidence_to_create") or []
    transcript_rel = str(transcript_path.relative_to(vault))
    entity_names = _entity_canonical_names(writer)
    for entry in evidence_items:
        title = str(entry.get("title") or entry.get("summary") or "").strip()
        if not title:
            continue
        resolved_claims = resolve_claim_links(listify(entry.get("linked_claims")), claim_id_map or {})
        arena = _infer_domain(
            str(entry.get("arena") or ""),
            fallback="cross_arena",
            text=f"{title} {entry.get('summary') or ''}",
            entity_names=entity_names,
        )
        try:
            new_evidence(
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
                linked_claims=resolved_claims,
                linked_episodes=_merge_links(entry.get("linked_episodes"), [draft_rel]),
                confidence_basis=_basis_or_default(entry, "Auto-extracted from conversation"),
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.evidence", exc)


# ── Fanout: claims ────────────────────────────────────────────────────────────

def _create_claim_records(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    db_path: Path | None = None,
) -> dict[str, str]:
    claims = writer.get("claims_to_create") or []
    claim_id_map: dict[str, str] = {}
    entity_names = _entity_canonical_names(writer)
    for entry in claims:
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
        try:
            created = new_claim(
                vault=vault,
                claim_text=claim_text,
                claim_class=str(entry.get("claim_class") or "interpretation").strip(),
                owner=str(entry.get("owner") or "user").strip(),
                status=str(entry.get("status") or "active").strip(),
                confidence=confidence,
                supporting_evidence=listify(entry.get("supporting_evidence")),
                contradicting_evidence=listify(entry.get("contradicting_evidence")),
                linked_patterns=listify(entry.get("linked_patterns")),
                first_seen=str(entry.get("first_seen") or "").strip() or None,
                last_reviewed=str(entry.get("last_reviewed") or "").strip() or None,
                review_notes=str(entry.get("review_notes") or "").strip(),
                arena=arena,
                compartments=list(entry.get("compartments") or []),
                privacy=str(entry.get("privacy") or "personal").strip(),
                significance=str(entry.get("significance") or "low").strip(),
                summary=str(entry.get("summary") or claim_text[:120]),
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
            frontmatter = writer.get("frontmatter") or {}
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
