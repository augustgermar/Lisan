from __future__ import annotations

from pathlib import Path
from typing import Any

from .log import log_capture, log_error
from .memory_pipeline import run_memory_pipeline
from .tracing import record_jobs_queued


def capture_text(
    vault: Path,
    text: str,
    conversation_id: str | None = None,
    speaker: str = "USER",
    provider: str | None = None,
    model: str | None = None,
    conversation_policy: dict[str, Any] | None = None,
    queue_background: bool = True,
    db_path: Path | None = None,
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
    if queue_background and result.action != "skip":
        from .jobs import enqueue_job
        from .job_policy import which_jobs_for_turn

        queued_jobs: list[dict[str, Any]] = []
        turn_metadata = {
            "vault": str(vault),
            "db_path": str(db_path) if db_path else None,
            "conversation_id": conversation_id,
            "text": text,
            "action": result.action,
            "mode": result.mode,
            "reason": "memory capture wrote or updated records",
            "listener": result.listener,
            "writer": result.writer or {},
            "draft_path": str(result.draft_path or ""),
            "transcript_path": str(result.transcript_path),
            "records_written": _count_created_records(result.writer or {}),
            "high_salience": _is_high_salience(result.listener, result.writer or {}),
            "self_analysis_requested": _is_self_analysis_requested(text),
            "explicit_memory_request": _is_memory_request(text),
        }
        job_specs = which_jobs_for_turn(turn_metadata, db_path=db_path)
        for job_spec in job_specs:
            if isinstance(job_spec, dict):
                job_type = str(job_spec.get("job_type") or "")
                payload = job_spec.get("payload") or {}
                priority = int(job_spec.get("priority") or 100)
            else:
                job_type, payload, priority = job_spec
            try:
                job_id = enqueue_job(job_type, payload, priority=priority, db_path=db_path)
                queued_jobs.append({"job_type": job_type, "job_id": job_id})
            except Exception:
                continue
        record_jobs_queued(len(queued_jobs))
        out["queued_jobs"] = queued_jobs
    log_capture(vault, text, out)
    return out


def _count_created_records(writer: dict[str, Any]) -> int:
    count = 0
    for key in ("evidence_to_create", "claims_to_create", "claims", "open_loops_to_create", "decisions_to_create"):
        value = writer.get(key)
        if isinstance(value, list):
            count += len(value)
    return count


def _is_high_salience(listener: dict[str, Any], writer: dict[str, Any]) -> bool:
    reasons = [str(reason).lower() for reason in (listener.get("reason") or []) if reason]
    if any("high-risk" in reason for reason in reasons):
        return True
    if str(writer.get("significance") or "").lower() == "high":
        return True
    return False


def _is_self_analysis_requested(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in ["self-analysis", "analyze myself", "analyze me", "what pattern", "recurring pattern"])


def _is_memory_request(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered.startswith("/remember") or lowered.startswith("/forget") or "remember this" in lowered or "fact correction" in lowered
