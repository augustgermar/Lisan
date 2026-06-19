from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from .log import log_capture, log_error
from .memory_pipeline import run_memory_pipeline
from .tracing import (
    finalize_turn_trace,
    get_current_turn_trace,
    reset_current_turn_trace,
    start_turn_trace,
)
from ..paths import sqlite_path
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
    append_response_to_transcript: bool = False,
) -> dict[str, Any]:
    trace = None
    trace_token = None
    created_trace = False
    if get_current_turn_trace() is None:
        turn_id = f"capture.{time.strftime('%Y%m%d%H%M%S')}.{uuid.uuid4().hex[:8]}"
        trace, trace_token = start_turn_trace(turn_id, text, "capture", False)
        created_trace = True
    try:
        result = run_memory_pipeline(
            vault=vault,
            text=text,
            conversation_id=conversation_id,
            speaker=speaker,
            provider=provider,
            model=model,
            conversation_policy=conversation_policy,
            db_path=db_path,
        )
    except Exception as exc:
        log_error(vault, "capture_text", exc)
        raise
    try:
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
        response_text = _extract_capture_response(result)
        if append_response_to_transcript and response_text:
            from .transcripts import append_transcript

            append_transcript(vault=vault, conversation_id=conversation_id, speaker="LISAN", text=response_text)
        out["response"] = response_text
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
        if created_trace and trace is not None:
            finalized = finalize_turn_trace(trace, db_path=db_path or sqlite_path(), vault=vault)
            out["trace_summary"] = finalized.summary()
            out["trace"] = finalized.as_dict()
        log_capture(vault, text, out)
        return out
    finally:
        if created_trace:
            reset_current_turn_trace(trace_token)


def _extract_capture_response(result: Any) -> str:
    elicitor = getattr(result, "elicitor", None) or {}
    response_text = str(elicitor.get("response") or "").strip()
    if not response_text:
        interlocutor = getattr(result, "interlocutor", None) or {}
        response_text = str(interlocutor.get("response") or "").strip()
    return response_text


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
