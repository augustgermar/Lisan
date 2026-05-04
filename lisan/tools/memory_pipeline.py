from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents import AssemblerAgent, InterlocutorAgent, ListenerAgent, SkepticAgent, WriterAgent
from ..frontmatter import write_markdown
from ..utils import slugify, today_iso
from .elicitor_session import run_elicitor_session
from .firewall import scan_text
from .log import log_error
from .narrative_state import load_narrative_state
from .record_factory import STATE_TTLS, new_decision, new_entity, new_open_loop, upsert_state
from .transcripts import append_transcript


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


def run_memory_pipeline(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
    conversation_policy: dict[str, Any] | None = None,
) -> MemoryPipelineResult:
    transcript_path = append_transcript(vault=vault, conversation_id=conversation_id, speaker=speaker, text=text)
    fw = scan_text(text, vault=vault)
    text = fw.text  # use sanitized version for all downstream agents
    prior_state = load_narrative_state(vault=vault, conversation_id=conversation_id)
    listener = ListenerAgent(vault=vault).run_json(text, provider=provider, model=model)
    action = str(listener.get("action", "skip"))
    mode = str(listener.get("mode", "skip"))

    # If we're mid-conversation (turn_count > 0, topic not closed), never fully skip a turn —
    # the user may be continuing a thread the heuristic can't see from the text alone.
    if (
        action == "skip"
        and prior_state.turn_count > 0
        and prior_state.mode_status not in ("closed",)
    ):
        action = "lightweight"
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

    context = AssemblerAgent(vault=vault).run(text).text
    task = _choose_task(text=text, listener=listener)
    writer = WriterAgent(vault=vault).run_json(
        text,
        significance="high" if action == "full" else "medium",
        provider=provider,
        model=model,
        task=task,
        context=context,
        transcript=str(transcript_path.relative_to(vault)),
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    skeptic = SkepticAgent(vault=vault).run_json(
        json.dumps(writer, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    interlocutor = InterlocutorAgent(vault=vault).run_json(
        json.dumps({"writer": writer, "skeptic": skeptic, "listener": listener}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
        conversation_policy=json.dumps(conversation_policy or {}, indent=2, ensure_ascii=True),
    )
    draft_path = _write_draft(vault, text, transcript_path, listener, writer, skeptic, interlocutor, task, mode, action)
    _create_entity_stubs(vault, writer, str(draft_path.relative_to(vault)))
    _apply_state_updates(vault, writer)
    _create_open_loops(vault, writer)
    _create_decisions(vault, writer)
    return MemoryPipelineResult(
        transcript_path=transcript_path,
        draft_path=draft_path,
        listener=listener,
        writer=writer,
        skeptic=skeptic,
        interlocutor=interlocutor,
        action=action,
        mode=mode,
    )


def _choose_task(text: str, listener: dict[str, Any]) -> str:
    # Primary: use the LLM's explicit memory type classification
    memory_type = str(listener.get("memory_type") or "").lower()
    if memory_type in ("decision", "open_loop", "state", "knowledge", "entity"):
        return memory_type
    return "episode"


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
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    slug = slugify(str(writer.get("summary") or text[:48]))[:80]
    path = vault / "drafts" / f"{today_iso()}-{timestamp}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": f"draft.memory.{timestamp}.{slug}",
        "type": "draft",
        "created": today_iso(),
        "updated": today_iso(),
        "status": "pending",
        "significance": str(writer.get("significance", "medium")),
        "arena_primary": "cross_arena",
        "arena_secondary": [],
        "privacy": "personal",
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": str(writer.get("summary") or text[:120]),
        "links": [str(transcript_path.relative_to(vault))],
        "confidence": str(writer.get("frontmatter", {}).get("confidence", "low")),
        "confidence_basis": str(writer.get("frontmatter", {}).get("confidence_basis", "Deterministic memory pipeline")),
        "last_confirmed": today_iso(),
        "review_after": today_iso(),
        "pipeline": {"action": action, "mode": mode, "task": task},
        "source": mode,
    }
    body = _render_draft_body(text, listener, writer, skeptic, interlocutor, task)
    write_markdown(path, frontmatter, body)
    return path


def _render_draft_body(
    text: str,
    listener: dict[str, Any],
    writer: dict[str, Any],
    skeptic: dict[str, Any],
    interlocutor: dict[str, Any],
    task: str,
) -> str:
    return f"""# Memory Draft

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


def _create_open_loops(vault: Path, writer: dict[str, Any]) -> None:
    """Materialize open loops immediately — open loops are always capture_now per spec."""
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
                confidence_basis="Auto-extracted from conversation",
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.open_loop", exc)


def _apply_state_updates(vault: Path, writer: dict[str, Any]) -> None:
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
                confidence_basis="Auto-extracted from conversation",
            )
        except Exception as exc:
            log_error(vault, "memory_pipeline.state_update", exc)


def _create_entity_stubs(vault: Path, writer: dict[str, Any], draft_rel_path: str) -> None:
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
                confidence_basis="Auto-extracted from conversation",
            )
        except FileExistsError:
            pass  # entity already exists — skip silently


def _create_decisions(vault: Path, writer: dict[str, Any]) -> None:
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
                confidence_basis="Auto-extracted from conversation",
                alternatives_considered=alternatives,
                revisit_conditions=revisit,
            )
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "memory_pipeline.decision", exc)
