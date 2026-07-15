"""Terminal job failures escalate to the owner — never silently.

Policy (owner-ratified, 2026-07-15): a silent failure is the worst kind.
The nine-day dead daily prompt proved it — every morning a job failed
terminally, respawned itself broken, and nothing told anyone. From now on
a terminal failure walks a fixed, loud ladder:

1. First terminal failure: message the owner over Telegram with the real
   error, and enqueue exactly one second-chance run of the same payload.
2. Second-chance failure: message the owner again and file an
   investigation open loop (``origin: self``) so the cause becomes work
   the system owes an answer on, visible to the drive layer.

The second-chance job is marked in its payload (``second_chance_of``) so
it can never spawn a third attempt: the ladder has two rungs, then it
stops and the investigation stands. Investigation loops are deduplicated
by a failure fingerprint — a series that keeps failing the same way adds
noise on Telegram (deliberately) but only one open investigation.

Everything here is best-effort by design: escalation must never take the
worker down with it. A failed notification is logged and the ladder
continues — filing the investigation matters more than the ping.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..paths import vault_root

# Payload key marking a second-chance run; its presence ends the ladder.
SECOND_CHANCE_KEY = "second_chance_of"

_FINGERPRINT_MAX = 80


def failure_fingerprint(job_type: str, error: str) -> str:
    """Stable identity of a failure mode: job type plus normalized error.

    Variable fragments (paths, ids, numbers) are stripped so tomorrow's
    instance of the same defect maps to the same open investigation.
    """
    text = str(error or "").lower()
    text = re.sub(r"job\.[0-9t]+\.[0-9a-f]+", "", text)  # job ids
    text = re.sub(r"[0-9]+", "", text)
    text = re.sub(r"[^a-z]+", "-", text).strip("-")
    slug = f"{job_type.replace('.', '-')}-{text}"[:_FINGERPRINT_MAX].strip("-")
    return slug or job_type.replace(".", "-")


def _notify_owner(text: str, *, chat_id: int | None, vault: Path) -> bool:
    from .log import log_error
    from .scheduler import _deliver_owner_message

    try:
        _deliver_owner_message(text, chat_id=chat_id)
        return True
    except Exception as exc:
        log_error(vault, "escalation.notify", exc)
        return False


def _job_description(job: dict[str, Any]) -> str:
    """A one-line human description of what the job was trying to do."""
    from .scheduler import task_body

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = str(job.get("job_type") or "")
    body = task_body(job_type, payload) if job_type.startswith("task.") else ""
    if not body:
        body = str(payload.get("conversation_id") or payload.get("path") or "").strip()
    if len(body) > 120:
        body = body[:117] + "..."
    return f"{job_type}" + (f" — {body}" if body else "")


def _active_investigation_exists(vault: Path, fingerprint: str) -> bool:
    from ..frontmatter import load_markdown

    root = vault / "open_loops"
    if not root.exists():
        return False
    for path in root.glob("*.md"):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if (
            str(fm.get("failure_fingerprint") or "") == fingerprint
            and str(fm.get("status") or "") == "active"
        ):
            return True
    return False


def _file_investigation(
    vault: Path,
    job: dict[str, Any],
    error: str,
    *,
    db_path: Path | None,
) -> Path | None:
    """The second failure becomes work: an open loop the drive layer sees.

    Uses ``failure_fingerprint`` (not ``deviation_fingerprint``) so the
    deviation scan's satiation pass never auto-closes an investigation it
    doesn't know how to verify.
    """
    import json

    from .log import log_error
    from .record_factory import new_open_loop

    job_type = str(job.get("job_type") or "")
    fingerprint = failure_fingerprint(job_type, error)
    if _active_investigation_exists(vault, fingerprint):
        return None
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    original_id = str(payload.get(SECOND_CHANCE_KEY) or "")
    try:
        record = new_open_loop(
            vault,
            title=f"Investigate repeated failure of {job_type}",
            summary=(
                f"{_job_description(job)} failed twice, retry included. "
                f"Last error: {str(error)[:300]}"
            ),
            significance="medium",
            priority="high",
            owner="agent",
            confidence="high",
            confidence_basis="Two terminal job failures, deterministic record",
            next_action=(
                f"Find the cause. Read logs/errors.log around the failure time, then "
                f"`lisan jobs show {job.get('id')}`"
                + (f" and `lisan jobs show {original_id}`" if original_id else "")
                + " for the payloads and full errors."
            ),
        )
    except FileExistsError:
        return None
    except Exception as exc:
        log_error(vault, "escalation.investigation", exc)
        return None
    path = record.path
    # Stamp the fingerprint and origin so dedupe and the self-loop scan work.
    try:
        from ..frontmatter import load_markdown, write_markdown

        doc = load_markdown(path)
        write_markdown(
            path,
            {
                **dict(doc.frontmatter),
                "origin": "self",
                "failure_fingerprint": fingerprint,
            },
            doc.body
            + "\n## Failure record\n\n"
            + f"- failed job: `{job.get('id')}`\n"
            + (f"- first attempt: `{original_id}`\n" if original_id else "")
            + f"- error: {str(error)[:500]}\n"
            + f"- payload: `{json.dumps(payload, ensure_ascii=True)[:500]}`\n",
        )
    except Exception as exc:
        log_error(vault, "escalation.investigation", exc)
    _index_quietly(path, vault, db_path)
    return path


def _index_quietly(path: Path, vault: Path, db_path: Path | None) -> None:
    try:
        from .rebuild_index import reindex_record

        reindex_record(path, vault, db_path, quiet=True)
    except Exception:
        pass


def escalate_terminal_failure(
    job: dict[str, Any],
    error: str,
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Walk the ladder for one terminally failed job. Never raises."""
    from .log import log_error

    vault = vault or vault_root()
    out: dict[str, Any] = {"notified": False, "second_chance_id": None, "investigation": None}
    try:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        chat_id = payload.get("chat_id")
        chat_id = int(chat_id) if chat_id is not None else None
        job_type = str(job.get("job_type") or "")
        description = _job_description(job)

        if payload.get(SECOND_CHANCE_KEY):
            # Rung 2: the retry failed too. Investigation, not a third try.
            investigation = _file_investigation(vault, job, error, db_path=db_path)
            out["investigation"] = str(investigation) if investigation else None
            note = (
                f"filed an investigation: {investigation.name}"
                if investigation
                else "an investigation for this failure is already open"
            )
            out["notified"] = _notify_owner(
                f"🚨 The retry failed too: {description}\n"
                f"Error: {str(error)[:400]}\n"
                f"I've {note}. I won't retry again on my own.",
                chat_id=chat_id,
                vault=vault,
            )
            return out

        # Rung 1: notify with the real error, then one second chance.
        second_chance_id: str | None = None
        second_chance_error: str | None = None
        try:
            from .jobs import enqueue_job

            second_chance_id = enqueue_job(
                job_type,
                {**payload, SECOND_CHANCE_KEY: str(job.get("id") or "")},
                priority=int(job.get("priority") or 100),
                max_attempts=1,
                db_path=db_path,
            )
            out["second_chance_id"] = second_chance_id
        except Exception as exc:
            # e.g. a bodyless task payload that can't be re-enqueued at all:
            # skip straight to the investigation — retrying is impossible.
            second_chance_error = str(exc)
            log_error(vault, "escalation.second_chance", exc)
            investigation = _file_investigation(vault, job, error, db_path=db_path)
            out["investigation"] = str(investigation) if investigation else None

        if second_chance_id:
            message = (
                f"⚠️ A background task of mine failed: {description}\n"
                f"Error: {str(error)[:400]}\n"
                f"I'm retrying it once now and will report if that fails too."
            )
        else:
            message = (
                f"⚠️ A background task of mine failed and cannot be retried: {description}\n"
                f"Error: {str(error)[:400]}\n"
                f"Retry was impossible ({str(second_chance_error)[:200]}); "
                f"I've filed an investigation."
            )
        out["notified"] = _notify_owner(message, chat_id=chat_id, vault=vault)
        return out
    except Exception as exc:
        log_error(vault, "escalation", exc)
        return out
