from __future__ import annotations

from pathlib import Path
from typing import Any

from .log import log_capture, log_error
from .memory_pipeline import run_memory_pipeline


def capture_text(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
    conversation_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        result = run_memory_pipeline(
            vault=vault,
            text=text,
            conversation_id=conversation_id,
            speaker=speaker,
            provider=provider,
            model=model,
            conversation_policy=conversation_policy,
        )
    except Exception as exc:
        log_error(vault, "capture_text", exc)
        raise
    out = {
        "transcript_path": str(result.transcript_path),
        "draft_path": str(result.draft_path or ""),
        "mode": result.mode,
        "action": result.action,
        "listener": result.listener,
        "writer": result.writer or {},
        "skeptic": result.skeptic or {},
        "interlocutor": result.interlocutor or {},
        "elicitor": result.elicitor or {},
        "narrative_state_path": str(result.narrative_state_path or ""),
        "narrative_state": result.narrative_state or {},
        "conversation_policy": conversation_policy or {},
    }
    log_capture(vault, text, out)
    return out
