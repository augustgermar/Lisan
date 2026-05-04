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
) -> MemoryPipelineResult:
    transcript_path = append_transcript(vault=vault, conversation_id=conversation_id, speaker=speaker, text=text)
    listener = ListenerAgent(vault=vault).run_json(text, provider=provider, model=model)
    action = str(listener.get("action", "skip"))
    mode = str(listener.get("mode", "skip"))
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
    )
    skeptic = SkepticAgent(vault=vault).run_json(json.dumps(writer, indent=2, ensure_ascii=True), significance="medium", provider=provider, model=model)
    interlocutor = InterlocutorAgent(vault=vault).run_json(
        json.dumps({"writer": writer, "skeptic": skeptic, "listener": listener}, indent=2, ensure_ascii=True),
        significance="medium",
        provider=provider,
        model=model,
    )
    draft_path = _write_draft(vault, text, transcript_path, listener, writer, skeptic, interlocutor, task, mode, action)
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
    reasons = set(listener.get("reason", []))
    if "decision phrase" in reasons:
        return "decision"
    if "open loop phrase" in reasons:
        return "open_loop"
    lowered = text.lower()
    if any(p in lowered for p in ["i decided", "going forward", "from now on", "the decision was"]):
        return "decision"
    if any(p in lowered for p in ["remind me to", "don't let me forget", "i need to follow up"]):
        return "open_loop"
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
    slug = slugify(str(writer.get("summary") or text[:48]))
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
