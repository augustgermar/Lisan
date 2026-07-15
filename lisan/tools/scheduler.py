"""Real-time task scheduling on top of the durable job queue.

The jobs table is the single source of truth for *what* runs and *when*
(``scheduled_for``, ``recurrence``); this module supplies the pieces around it:

- deterministic parsing of "when" expressions and recurrence rules,
- ``schedule_task`` — the one entry point for creating future tasks
  (used by the CLI and the interlocutor's ``schedule_task`` tool),
- execution of the ``task.*`` job types (reminder / prompt / codex),
- owner-only message delivery over Telegram,
- ``run_scheduler_loop`` — the resident tick that notices due rows within
  seconds. It runs as a thread inside the Telegram service and standalone
  via ``lisan scheduler run``. The OS keep-alive layer (launchd/systemd)
  never holds task state; a dead scheduler just means late execution, and
  the loop catches up on everything past-due when it comes back.

Stdlib only. All stored timestamps are UTC; user-facing "when" expressions
and ``daily@HH:MM`` recurrences are interpreted in the system local zone.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from .db import connect as _db_connect

from ..config import load_config
from ..paths import sqlite_path

TASK_JOB_TYPES = {"task.reminder", "task.prompt", "task.run_codex"}

_TASK_KINDS = {
    "reminder": "task.reminder",
    "prompt": "task.prompt",
    "codex": "task.run_codex",
}

# Canonical payload key for each task type, per schedule_task. Jobs written
# by other hands (the executor once created a task.prompt whose body sat
# under "text", killing the owner's daily prompt for nine straight days)
# must still fire: a task whose body is findable under any known key runs.
_TASK_BODY_KEYS = {
    "task.reminder": ("message", "text", "prompt", "task"),
    "task.prompt": ("prompt", "text", "message", "task"),
    "task.run_codex": ("task", "text", "prompt", "message"),
}


def task_body(job_type: str, payload: dict[str, Any]) -> str:
    """The task's body text, canonical key first, known aliases after."""
    for key in _TASK_BODY_KEYS.get(job_type, ()):
        body = str(payload.get(key) or "").strip()
        if body:
            return body
    return ""


def normalize_task_payload(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a task payload onto its canonical body key, dropping aliases.

    Called at enqueue time so the stored row is always canonical no matter
    who created it. Raises when no body is present under any known key —
    a bodyless task must fail at creation, where someone is watching, not
    at fire time where the failure respawns daily.
    """
    keys = _TASK_BODY_KEYS.get(job_type)
    if not keys:
        return payload
    body = task_body(job_type, payload)
    if not body:
        raise ValueError(f"{job_type} requires a non-empty body (checked keys: {', '.join(keys)})")
    normalized = {key: value for key, value in payload.items() if key not in keys}
    normalized[keys[0]] = body
    return normalized

# How far past due a delivery may be before we say so explicitly.
_LATE_THRESHOLD = timedelta(minutes=15)

_EVERY_RE = re.compile(r"^every:(\d+)([smhdw])$")
_DAILY_RE = re.compile(r"^daily@([01]?\d|2[0-3]):([0-5]\d)$")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_display(dt: datetime) -> str:
    return dt.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M %Z")


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse an absolute "when" expression into an aware UTC datetime.

    Accepted forms (naive datetimes are read as system local time):
    - ISO datetime: ``2026-07-09T15:00``, ``2026-07-09 15:00[:SS][+TZ]``
    - Bare time ``HH:MM`` — today at that time, or tomorrow if already past.
    - ``tomorrow HH:MM``
    - Relative offset ``+<N><s|m|h|d|w>``, e.g. ``+2h``, ``+30m``.

    Fuzzy phrases ("next Thursday") are deliberately rejected — the caller
    (model or human) resolves those to one of the deterministic forms.
    """
    now = now or _now_utc()
    text = str(value or "").strip().lower()
    if not text:
        raise ValueError(f"empty 'when'; current local time is {_local_display(now)}")

    offset = re.match(r"^\+(\d+)([smhdw])$", text)
    if offset:
        return now + timedelta(seconds=int(offset.group(1)) * _UNIT_SECONDS[offset.group(2)])

    tomorrow = re.match(r"^tomorrow\s+([01]?\d|2[0-3]):([0-5]\d)$", text)
    if tomorrow:
        local_now = now.astimezone(_local_tz())
        candidate = (local_now + timedelta(days=1)).replace(
            hour=int(tomorrow.group(1)), minute=int(tomorrow.group(2)), second=0, microsecond=0
        )
        return candidate.astimezone(timezone.utc)

    bare_time = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
    if bare_time:
        local_now = now.astimezone(_local_tz())
        candidate = local_now.replace(
            hour=int(bare_time.group(1)), minute=int(bare_time.group(2)), second=0, microsecond=0
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"could not parse 'when' {text!r}; use 'YYYY-MM-DD HH:MM' (local time) or ISO 8601. "
            f"Current local time is {_local_display(now)}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz())
    return parsed.astimezone(timezone.utc)


def normalize_recurrence(value: str | None) -> str | None:
    """Validate and normalize a recurrence rule. Two deterministic forms:
    ``every:<N><s|m|h|d|w>`` and ``daily@HH:MM`` (local time)."""
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    match = _EVERY_RE.match(text)
    if match:
        if int(match.group(1)) < 1:
            raise ValueError(f"recurrence interval must be positive: {value!r}")
        return text
    if _DAILY_RE.match(text):
        hour, minute = text.split("@", 1)[1].split(":")
        return f"daily@{int(hour):02d}:{minute}"
    raise ValueError(
        f"unsupported recurrence {value!r}; use 'every:<N><m|h|d|w>' (e.g. every:30m) "
        "or 'daily@HH:MM' (local time)"
    )


def next_occurrence(recurrence: str, *, after: datetime | None = None) -> datetime:
    """First fire time strictly after ``after`` (default: now), in UTC."""
    after = (after or _now_utc()).astimezone(timezone.utc)
    rule = normalize_recurrence(recurrence)
    if rule is None:
        raise ValueError("next_occurrence requires a recurrence rule")
    match = _EVERY_RE.match(rule)
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        return after + timedelta(seconds=seconds)
    hour, minute = (int(part) for part in rule.split("@", 1)[1].split(":"))
    local_after = after.astimezone(_local_tz())
    candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_after:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def schedule_task(
    *,
    kind: str,
    text: str,
    when: str | datetime | None = None,
    recurrence: str | None = None,
    chat_id: int | None = None,
    working_directory: str | None = None,
    conversation_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a scheduled task. Returns a small summary dict.

    ``when`` may be omitted for recurring tasks (first fire = next
    occurrence). Past times are rejected rather than fired immediately —
    a silently-instant "future" task is almost always a caller mistake.
    """
    from .jobs import enqueue_job

    kind_key = str(kind or "").strip().lower()
    job_type = _TASK_KINDS.get(kind_key)
    if job_type is None:
        raise ValueError(f"unknown task kind {kind!r}; expected one of {sorted(_TASK_KINDS)}")
    body = str(text or "").strip()
    if not body:
        raise ValueError("task text must not be empty")

    rule = normalize_recurrence(recurrence)
    now = _now_utc()
    if when is None or (isinstance(when, str) and not when.strip()):
        if rule is None:
            raise ValueError("either 'when' or a recurrence rule is required")
        fire_at = next_occurrence(rule, after=now)
    elif isinstance(when, datetime):
        fire_at = (when if when.tzinfo else when.replace(tzinfo=_local_tz())).astimezone(timezone.utc)
    else:
        fire_at = parse_when(when, now=now)
    if fire_at < now - timedelta(seconds=60):
        raise ValueError(
            f"'when' resolves to the past ({_local_display(fire_at)}); "
            f"current local time is {_local_display(now)}"
        )

    payload: dict[str, Any] = {"due": _to_iso(fire_at)}
    if chat_id is not None:
        payload["chat_id"] = int(chat_id)
    if conversation_id:
        payload["conversation_id"] = str(conversation_id)
    if job_type == "task.reminder":
        payload["message"] = body
    elif job_type == "task.prompt":
        payload["prompt"] = body
    else:
        payload["task"] = body
        if working_directory:
            payload["working_directory"] = str(working_directory)

    job_id = enqueue_job(
        job_type,
        payload,
        scheduled_for=fire_at,
        recurrence=rule,
        max_attempts=3,
        db_path=db_path,
    )
    return {
        "job_id": job_id,
        "kind": kind_key,
        "job_type": job_type,
        "scheduled_for": _to_iso(fire_at),
        "scheduled_for_local": _local_display(fire_at),
        "recurrence": rule,
    }


def cancel_task(job_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    from .jobs import cancel_job

    return cancel_job(job_id, db_path=db_path)


def list_tasks(*, db_path: Path | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Scheduled tasks: everything pending, plus recently finished ones."""
    from .jobs import list_jobs

    tasks = [job for job in list_jobs(limit=5000, db_path=db_path) if job.get("job_type") in TASK_JOB_TYPES]
    pending = [job for job in tasks if job.get("status") in {"queued", "retry_wait", "running"}]
    finished = [job for job in tasks if job.get("status") not in {"queued", "retry_wait", "running"}]
    pending.sort(key=lambda job: str(job.get("scheduled_for") or ""))
    finished.sort(key=lambda job: str(job.get("finished_at") or ""), reverse=True)
    return (pending + finished)[:limit]


def format_task_list(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for job in tasks:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        body = str(payload.get("message") or payload.get("prompt") or payload.get("task") or "").strip()
        if len(body) > 60:
            body = body[:57] + "..."
        when = str(job.get("scheduled_for") or "").strip()
        if when:
            parsed = _parse_iso(when)
            when = _local_display(parsed) if parsed else when
        recurrence = str(job.get("recurrence") or "").strip()
        parts = [
            f"{job.get('id')}",
            f"[{job.get('status')}]",
            str(job.get("job_type") or "").removeprefix("task."),
            f"at {when}" if when else "",
            f"({recurrence})" if recurrence else "",
            f"— {body}" if body else "",
        ]
        lines.append("  ".join(part for part in parts if part))
    return "\n".join(lines)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── Task execution (dispatch targets for the task.* job types) ──────────────

def run_task_job(
    job: dict[str, Any],
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    send_fn: Callable[[str, int | None], Any] | None = None,
) -> dict[str, Any]:
    """Execute one task.* job. ``send_fn(text, chat_id)`` overrides delivery
    (used by the in-bot scheduler thread and tests); default is the Telegram
    API resolved from config."""
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = str(job.get("job_type") or "")
    chat_id = payload.get("chat_id")
    chat_id = int(chat_id) if chat_id is not None else None
    deliver = send_fn or (lambda text, cid: _deliver_owner_message(text, chat_id=cid, config=config))

    if job_type == "task.reminder":
        message = task_body(job_type, payload)
        if not message:
            raise ValueError("task.reminder requires a message")
        deliver(_with_late_note(f"⏰ Reminder: {message}", payload), chat_id)
        return {"delivered": True, "message": message}

    if job_type == "task.prompt":
        from .chat import _process_chat_turn

        prompt = task_body(job_type, payload)
        if not prompt:
            raise ValueError("task.prompt requires a prompt")
        conversation_id = f"scheduled-{job.get('id')}"
        result = _process_chat_turn(
            vault=vault,
            conversation_id=conversation_id,
            text=prompt,
            provider=provider,
            model=model,
            db_path=db_path,
        )
        response = str(result.get("response") or "").strip()
        if not response:
            error = str(result.get("error") or "the pipeline produced no response").strip()
            response = f"The scheduled task ran but failed: {error}"
        # Delivery IS the task for a scheduled prompt — an undelivered
        # response is a silent failure. Re-running the pipeline on retry is
        # cheap and safe, unlike codex below, so let a delivery error fail
        # the job and walk the escalation ladder.
        deliver(_with_late_note(f"⏰ Scheduled: {prompt}\n\n{response}", payload), chat_id)
        return {"response": response, "delivered": True, "conversation_id": conversation_id}

    if job_type == "task.run_codex":
        from .execution_tools import run_codex

        task = task_body(job_type, payload)
        if not task:
            raise ValueError("task.run_codex requires a task")
        # The owner approved this task when scheduling it; firing later must
        # not block on an interactive prompt that has nobody at the keyboard.
        result_text = run_codex(
            task,
            working_directory=payload.get("working_directory") or None,
            vault=vault,
            config=config or load_config(),
            db_path=db_path,
            provider=provider,
            model=model,
            approval_fn=lambda _name, _args: True,
        )
        summary = result_text if len(result_text) <= 2000 else result_text[:1997] + "..."
        delivered = _best_effort_deliver(deliver, _with_late_note(f"⏰ Scheduled codex task: {task}\n\n{summary}", payload), chat_id)
        return {"result": result_text, "delivered": delivered}

    raise ValueError(f"not a task job type: {job_type}")


def _with_late_note(text: str, payload: dict[str, Any]) -> str:
    due = _parse_iso(str(payload.get("due") or ""))
    if due and _now_utc() - due > _LATE_THRESHOLD:
        return f"{text}\n\n(This was due {_local_display(due)} — delivering late.)"
    return text


def _best_effort_deliver(deliver: Callable[[str, int | None], Any], text: str, chat_id: int | None) -> bool:
    """Codex tasks did their real work already, with side effects a retry
    would repeat — a delivery hiccup surfaces in the job result instead of
    failing (and re-running) the whole job. Prompt tasks deliberately do
    NOT use this: their delivery is the task."""
    try:
        deliver(text, chat_id)
        return True
    except Exception:
        return False


def _deliver_owner_message(text: str, *, chat_id: int | None = None, config: dict[str, Any] | None = None) -> None:
    """Send a message to the owner over Telegram. Owner-only by construction:
    the target must be on the configured allowlist; anything else falls back
    to the first allowlisted id. This is deliberately the only outbound
    channel scheduled tasks can use (see the disclosure-gate roadmap)."""
    from .telegram_bot import _chunk, _resolve_settings, _telegram_api

    config = config or load_config()
    token, allowed = _resolve_settings(config)
    if not token or not allowed:
        raise RuntimeError(
            "telegram is not configured; cannot deliver a scheduled message. "
            "Run `lisan telegram setup`."
        )
    target = chat_id if chat_id is not None and chat_id in allowed else sorted(allowed)[0]
    for part in _chunk(text):
        response = _telegram_api(token, "sendMessage", {"chat_id": target, "text": part}, timeout=15)
        if not response.get("ok"):
            raise RuntimeError(f"telegram sendMessage failed: {response}")


# ── The resident scheduler loop ──────────────────────────────────────────────

def seconds_until_next_due(*, db_path: Path | None = None, ceiling: float = 30.0) -> float:
    """How long the loop may sleep: time to the earliest pending row, capped
    at ``ceiling`` so work scheduled by other processes is noticed promptly."""
    import sqlite3

    conn = _db_connect(db_path)
    try:
        try:
            immediate = conn.execute(
                "SELECT 1 FROM jobs WHERE status = 'queued' AND scheduled_for IS NULL LIMIT 1"
            ).fetchone()
            if immediate:
                return 0.0
            row = conn.execute(
                """
                SELECT MIN(scheduled_for) FROM jobs
                WHERE status IN ('queued', 'retry_wait') AND scheduled_for IS NOT NULL
                """
            ).fetchone()
        except sqlite3.OperationalError:
            return ceiling
    finally:
        conn.close()
    if not row or not row[0]:
        return ceiling
    due = _parse_iso(str(row[0]))
    if due is None:
        return ceiling
    delta = (due - _now_utc()).total_seconds()
    return min(max(delta, 0.0), ceiling)


def run_scheduler_loop(
    *,
    vault: Path,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    poll_seconds: float = 30.0,
    stop_event: threading.Event | None = None,
    max_ticks: int | None = None,
    send_fn: Callable[[str, int | None], Any] | None = None,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Tick until stopped: drain everything due, sleep until the next row.

    ``claim_next_job`` only returns rows whose ``scheduled_for`` has passed,
    so a tick never fires future work early — and after downtime the first
    tick naturally catches up on everything past-due. ``send_fn`` is threaded
    through to task delivery so the in-bot scheduler reuses the bot's session.
    """
    from .jobs import run_jobs_worker
    from .log import log_error

    stop_event = stop_event or threading.Event()
    ticks = 0
    while not stop_event.is_set():
        try:
            summary = _run_worker_with_delivery(
                vault=vault, db_path=db_path, provider=provider, model=model, send_fn=send_fn
            )
            if on_tick is not None and summary.get("processed_count"):
                on_tick(summary)
        except Exception as exc:
            try:
                log_error(vault, "scheduler tick failed", exc)
            except Exception:
                pass
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        stop_event.wait(seconds_until_next_due(db_path=db_path, ceiling=poll_seconds))
    return ticks


# ── Always-on service install ────────────────────────────────────────────────

_SCHEDULER_LABEL = "com.lisan.scheduler"
_SCHEDULER_UNIT = "lisan-scheduler.service"


def install_scheduler_service(*, vault: Path) -> int:
    """Install `lisan scheduler run` as an always-on OS service. Only needed
    when the Telegram service isn't running (it hosts the same loop)."""
    import sys

    from ..paths import repo_root
    from .service_install import ServiceSpec, install_service, service_path_env

    logs = vault / "logs"
    spec = ServiceSpec(
        label=_SCHEDULER_LABEL,
        unit_name=_SCHEDULER_UNIT,
        description="Lisan task scheduler",
        program_args=[sys.executable, "-m", "lisan", "scheduler", "run", "--vault", str(vault)],
        environment={"LISAN_VAULT": str(vault), "PATH": service_path_env()},
        working_directory=repo_root(),
        out_log=logs / "scheduler-service.out.log",
        err_log=logs / "scheduler-service.err.log",
    )
    return install_service(spec)


def uninstall_scheduler_service() -> int:
    from .service_install import uninstall_service

    return uninstall_service(label=_SCHEDULER_LABEL, unit_name=_SCHEDULER_UNIT)


_send_fn_local = threading.local()


def _run_worker_with_delivery(
    *,
    vault: Path,
    db_path: Path | None,
    provider: str | None,
    model: str | None,
    send_fn: Callable[[str, int | None], Any] | None,
) -> dict[str, Any]:
    from .jobs import run_jobs_worker

    _send_fn_local.value = send_fn
    try:
        return run_jobs_worker(vault=vault, db_path=db_path, provider=provider, model=model)
    finally:
        _send_fn_local.value = None


def current_send_fn() -> Callable[[str, int | None], Any] | None:
    """Delivery override installed by the loop for the duration of a drain
    (thread-local, so a bot-hosted scheduler and a CLI worker don't cross)."""
    return getattr(_send_fn_local, "value", None)
