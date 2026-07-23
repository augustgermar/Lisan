"""The Adjutant reporter: results re-enter through the front door.

The executor cannot write memory. It hands its structured result here,
and this module renders it as a capture turn — conversation_id
"adjutant", speaker "ADJUTANT" — so the Listener/Writer/Skeptic pipeline
triages it like any other input. The Skeptic is expected to flag
overclaiming ("script reports success but produced no output file");
nothing in this module softens a failure.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .adjutant_executor import ExecutionResult

ADJUTANT_CONVERSATION_ID = "adjutant"

_STDIO_EXCERPT_CHARS = 2000


def result_payload(result: ExecutionResult, *, verdict_path: str = "") -> dict[str, Any]:
    """The adjutant_result.schema.json shape, built from an ExecutionResult."""
    payload: dict[str, Any] = {
        "task_id": result.task_id,
        "kind": result.kind,
        "ok": result.ok,
        "actions": list(result.actions),
        "artifacts": list(result.artifacts),
        "findings": list(result.findings),
        "errors": list(result.errors),
        "confidence": result.confidence,
        "duration_seconds": result.duration_seconds,
    }
    if verdict_path:
        payload["verdict_path"] = verdict_path
    if result.exit_code is not None:
        payload["exit_code"] = result.exit_code
    return payload


def render_result_turn(result: ExecutionResult, *, verdict_path: str = "") -> str:
    lines = [
        f"ADJUTANT RESULT — task {result.task_id} ({result.kind}): "
        + ("success" if result.ok else "FAILURE"),
    ]
    if verdict_path:
        lines.append(f"Authorized by: {verdict_path}")
    if result.actions:
        lines.append("Actions taken:")
        lines.extend(f"- {action}" for action in result.actions)
    if result.artifacts:
        lines.append("Artifacts produced:")
        lines.extend(f"- {artifact}" for artifact in result.artifacts)
    if result.findings:
        lines.append(f"Findings ({len(result.findings)}):")
        for finding in result.findings[:20]:
            lines.append(f"- {finding}")
        if len(result.findings) > 20:
            lines.append(f"- ... and {len(result.findings) - 20} more (full list in artifacts)")
    if result.errors:
        lines.append("Errors (real causes, unsoftened):")
        lines.extend(f"- {error}" for error in result.errors)
    if result.exit_code is not None:
        lines.append(f"Exit code: {result.exit_code}; wall time {result.duration_seconds}s")
    for label, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        text = stream.strip()
        if text:
            excerpt = text[:_STDIO_EXCERPT_CHARS]
            suffix = " [truncated]" if len(text) > _STDIO_EXCERPT_CHARS else ""
            lines.append(f"{label}:\n{excerpt}{suffix}")
    lines.append(f"Reporter confidence: {result.confidence}")
    return "\n".join(lines)


def report_result(
    vault: Path,
    result: ExecutionResult,
    *,
    verdict_path: str = "",
    db_path: Path | None = None,
    capture: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Submit the result as a capture turn. ``capture`` is injectable for
    tests; the default is the real front door."""
    if capture is None:
        from .capture import capture_text as capture
    text = render_result_turn(result, verdict_path=verdict_path)
    return capture(
        vault=vault,
        text=text,
        conversation_id=ADJUTANT_CONVERSATION_ID,
        speaker="ADJUTANT",
        db_path=db_path,
    )
