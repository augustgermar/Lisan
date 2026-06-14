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
from .deixis import render_deixis
from .domain_fields import with_domain_fields
from .narrative_state import (
    conversation_history,
    format_history,
    load_narrative_state,
    save_narrative_state,
    update_narrative_state,
)
from .record_fanout import (
    fanout_claims,
    fanout_decisions,
    fanout_evidence,
    fanout_open_loops,
    fanout_state_updates,
)
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
    domain = str((conversation_policy or {}).get("domain_override") or (conversation_policy or {}).get("arena_override") or "") or None
    # v0.1.7: the cross-conversation "Recent Activity" preamble lives in the
    # assembler (retrieval.assemble_context) now, gated on whether the
    # conversation is fresh. That way the extraction path gets it too.
    context = assemble_context(text, domain=domain, vault=vault, conversation_id=conversation_id)
    # Deixis: the elicitor is a conversational consumer — it asks the principal
    # clarifying questions. Render assembled context + narrative state from role
    # tokens to second person ("you"/"I") before they reach the agent. The
    # conversation_history is the principal's raw first-person transcript — leave
    # it untouched.
    context = render_deixis(context, "interlocutor")
    elicitor = ElicitorAgent(vault=vault).run_json(
        text,
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
        current_state=render_deixis(json.dumps(state.as_dict(), indent=2, ensure_ascii=True), "interlocutor"),
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


_TOPIC_SHIFT_PHRASES = (
    "oh, also", "oh also", "by the way", "on a different note",
    "speaking of which", "before i forget", "one more thing",
    "can i ask you something", "can i ask about",
    "oh, i also", "oh and also", "anyway,", "so anyway",
    "switching topics", "different subject", "new topic",
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
    # Topic shift: user introduces a new subject mid-session. Only close if
    # the session already has established content worth preserving (turn_count
    # >= 2 with at least 1 established fact) — prevents premature closure on
    # the very first acknowledgment turn.
    turn_count = int(state.get("turn_count", 0))
    established = state.get("established") or []
    if turn_count >= 2 and len(established) >= 1:
        if any(p in lowered for p in _TOPIC_SHIFT_PHRASES):
            return True
    # Hard cap: after _MAX_ELICITOR_TURNS with enough established facts, hand off.
    if turn_count >= _MAX_ELICITOR_TURNS:
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
        provider_error_mode="raise",
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
        provider_error_mode="raise",
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    interlocutor = InterlocutorAgent(vault=vault).run_json(
        json.dumps({"writer": writer, "skeptic": skeptic, "narrative_state": state}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        provider_error_mode="raise",
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
        "domain_primary": "cross_arena",
        "domain_secondary": [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    draft_rel = str(path.relative_to(vault))
    # Evidence runs before claims so claim.supporting_evidence can be resolved
    # through evidence_id_map — mirrors the extraction path fanout order.
    evidence_id_map = fanout_evidence(vault, writer, transcript_path, draft_rel)
    fanout_claims(vault, writer, draft_rel, evidence_id_map=evidence_id_map)
    fanout_state_updates(vault, writer, draft_rel)
    fanout_open_loops(vault, writer, draft_rel)
    fanout_decisions(vault, writer, draft_rel)
    return path


