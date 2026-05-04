from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import ElicitorAgent, InterlocutorAgent, SkepticAgent, WriterAgent
from ..frontmatter import write_markdown
from ..utils import slugify, today_iso
from .assembler import assemble_context
from .log import log_error
from .narrative_state import (
    conversation_history,
    format_history,
    load_narrative_state,
    save_narrative_state,
    update_narrative_state,
)
from .record_factory import STATE_TTLS, new_decision
from .transcripts import append_transcript

# Force Writer handoff after this many turns when enough has been established.
# Prevents elicitor conversations from running forever without producing a draft.
_MAX_ELICITOR_TURNS = 12


@dataclass(slots=True)
class ElicitorSessionResult:
    transcript_path: Path
    state_path: Path
    response: dict[str, Any]
    narrative_state: dict[str, Any]
    draft_path: Path | None
    topic_closed: bool


def run_elicitor_session(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
    transcript_path: Path | None = None,
    conversation_policy: dict[str, Any] | None = None,
) -> ElicitorSessionResult:
    transcript_path = transcript_path or append_transcript(vault=vault, conversation_id=conversation_id, speaker=speaker, text=text)
    state = load_narrative_state(vault, conversation_id)
    history = conversation_history(vault, conversation_id)
    arena = str((conversation_policy or {}).get("arena_override") or "") or None
    context = assemble_context(text, arena=arena, vault=vault, conversation_id=conversation_id)
    elicitor = ElicitorAgent(vault=vault).run_json(
        text,
        significance="medium",
        provider=provider,
        model=model,
        current_state=json.dumps(state.as_dict(), indent=2, ensure_ascii=True),
        conversation_history=format_history(history),
        assembler_context=context,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    updated_state = update_narrative_state(state, text, elicitor)
    state_path = save_narrative_state(vault, updated_state)
    topic_closed = _topic_closed(text, elicitor, updated_state.as_dict())
    draft_path = None
    if topic_closed:
        draft_path = _write_elicitor_draft(
            vault=vault,
            text=text,
            transcript_path=transcript_path,
            state=updated_state.as_dict(),
            elicitor=elicitor,
            provider=provider,
            model=model,
            conversation_policy=conversation_policy,
        )
    return ElicitorSessionResult(
        transcript_path=transcript_path,
        state_path=state_path,
        response=elicitor,
        narrative_state=updated_state.as_dict(),
        draft_path=draft_path,
        topic_closed=topic_closed,
    )


def _topic_closed(text: str, elicitor: dict[str, Any], state: dict[str, Any]) -> bool:
    # Primary: trust the LLM's own mode assessment
    if str(state.get("mode_status", "")).lower() == "closed":
        return True
    elicitor_state = elicitor.get("updated_narrative_state", {})
    if str(elicitor_state.get("mode_status", "")).lower() == "closed":
        return True
    # Secondary: unambiguous explicit closure phrases
    lowered = text.lower()
    if any(p in lowered for p in ["moving on", "next topic", "let's move on", "change the subject"]):
        return True
    next_step = str(elicitor_state.get("next_step", "")).lower()
    if "handoff to writer" in next_step or "topic closed" in next_step:
        return True
    # Hard cap: after _MAX_ELICITOR_TURNS with enough established facts, hand off.
    # Avoids runaway sessions that never produce a draft.
    turn_count = int(state.get("turn_count", 0))
    if turn_count >= _MAX_ELICITOR_TURNS:
        established = state.get("established") or []
        if len(established) >= 3:
            return True
    return False


def _write_elicitor_draft(
    vault: Path,
    text: str,
    transcript_path: Path,
    state: dict[str, Any],
    elicitor: dict[str, Any],
    provider: str | None,
    model: str | None,
    conversation_policy: dict[str, Any] | None = None,
) -> Path:
    writer = WriterAgent(vault=vault).run_json(
        json.dumps(
            {
                "source": "elicitor",
                "transcript": str(transcript_path.relative_to(vault)),
                "narrative_state": state,
                "elicitor": elicitor,
                "source_text": text,
            },
            indent=2,
            ensure_ascii=True,
        ),
        significance="high",
        provider=provider,
        model=model,
        task="episode",
        source="elicitor",
        narrative_state=json.dumps(state, indent=2, ensure_ascii=True),
        transcript=str(transcript_path.relative_to(vault)),
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    skeptic = SkepticAgent(vault=vault).run_json(
        json.dumps({"writer": writer, "narrative_state": state, "elicitor": elicitor}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    interlocutor = InterlocutorAgent(vault=vault).run_json(
        json.dumps({"writer": writer, "skeptic": skeptic, "narrative_state": state}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    slug = slugify(str(writer.get("summary") or text[:48]))
    path = vault / "drafts" / f"{today_iso()}-{timestamp}-elicitor-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": f"draft.elicitor.{slug}",
        "type": "draft",
        "created": today_iso(),
        "updated": today_iso(),
        "status": "pending",
        "significance": str(writer.get("significance", "high")),
        "arena_primary": "cross_arena",
        "arena_secondary": [],
        "privacy": "personal",
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": str(writer.get("summary") or "Elicitor-derived draft"),
        "links": [str(transcript_path.relative_to(vault))],
        "confidence": str(writer.get("frontmatter", {}).get("confidence", "low")),
        "confidence_basis": str(writer.get("frontmatter", {}).get("confidence_basis", "Elicitor closure")),
        "last_confirmed": today_iso(),
        "review_after": today_iso(),
        "pipeline": {"action": "full", "mode": "elicitor", "task": "episode"},
        "source": "elicitor",
    }
    body = f"""# Elicitor Draft

## Narrative State

```json
{json.dumps(state, indent=2, ensure_ascii=True)}
```

## Listener

Elicitor mode closure was detected.

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
    write_markdown(path, frontmatter, body)
    _apply_elicitor_state_updates(vault, writer)
    _create_elicitor_open_loops(vault, writer)
    _create_elicitor_decisions(vault, writer)
    return path


def _create_elicitor_open_loops(vault: Path, writer: dict[str, Any]) -> None:
    from .record_factory import new_open_loop
    loops = writer.get("open_loops_to_create") or []
    for loop in loops:
        title = str(loop.get("title") or "").strip()
        next_action = str(loop.get("next_action") or "").strip()
        summary = str(loop.get("summary") or "").strip()
        priority = str(loop.get("priority") or "medium").strip()
        arena = str(loop.get("arena") or "cross_arena").strip()
        if not title or not next_action:
            continue
        if priority not in ("low", "medium", "high"):
            priority = "medium"
        try:
            new_open_loop(
                vault=vault,
                title=title,
                arena_primary=arena if arena in STATE_TTLS else "cross_arena",
                summary=summary or title,
                next_action=next_action,
                priority=priority,
                confidence="low",
                confidence_basis="Auto-extracted from elicitor conversation",
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "elicitor_session.open_loop", exc)


def _apply_elicitor_state_updates(vault: Path, writer: dict[str, Any]) -> None:
    from .record_factory import upsert_state
    updates = writer.get("state_updates") or []
    for update in updates:
        arena = str(update.get("arena") or "").strip().lower()
        summary = str(update.get("summary") or "").strip()
        confidence = str(update.get("confidence") or "low").strip()
        if not arena or not summary or arena not in STATE_TTLS:
            continue
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        try:
            upsert_state(
                vault=vault,
                arena_primary=arena,
                summary=summary,
                confidence=confidence,
                confidence_basis="Auto-extracted from elicitor conversation",
            )
        except Exception as exc:
            log_error(vault, "elicitor_session.state_update", exc)


def _create_elicitor_decisions(vault: Path, writer: dict[str, Any]) -> None:
    decisions = writer.get("decisions_to_create") or []
    for entry in decisions:
        title = str(entry.get("title") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        arena = str(entry.get("arena") or "cross_arena").strip()
        significance = str(entry.get("significance") or "low").strip()
        alternatives = list(entry.get("alternatives_considered") or [])
        revisit = list(entry.get("revisit_conditions") or [])
        if not title or not summary:
            continue
        if significance not in ("low", "medium", "high"):
            significance = "low"
        try:
            new_decision(
                vault=vault,
                title=title,
                arena_primary=arena if arena in STATE_TTLS else "cross_arena",
                summary=summary,
                significance=significance,
                confidence="low",
                confidence_basis="Auto-extracted from elicitor conversation",
                alternatives_considered=alternatives,
                revisit_conditions=revisit,
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "elicitor_session.decision", exc)
