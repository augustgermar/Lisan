from __future__ import annotations

from pathlib import Path

from .memory_pipeline import run_memory_pipeline


def capture_text(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    result = run_memory_pipeline(
        vault=vault,
        text=text,
        conversation_id=conversation_id,
        speaker=speaker,
        provider=provider,
        model=model,
    )
    return {
        "transcript_path": str(result.transcript_path),
        "draft_path": str(result.draft_path or ""),
        "mode": result.mode,
        "action": result.action,
        "listener": result.listener,
    }
