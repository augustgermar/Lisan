"""Retrospective capture sweep (vellum-assistant review, item 5 — made
deterministic the Lisan way).

Every finished exchange is supposed to get a ``capture.observe`` job, but
a crashed process, a killed eval run, or a failed enqueue can drop one —
and a turn that was never observed is a memory that silently never formed.
Vellum's retrospective re-reads the conversation with an LLM and asks it
to remember what was missed; ours doesn't need to guess: the transcript
says what was said, and the jobs table says what was observed. The sweep
diffs them and re-enqueues observe jobs for the gap.

No pointer state to maintain: the transcript + jobs tables ARE the state,
so the sweep is stateless, idempotent (a re-enqueued exchange is covered
on the next pass by its own job row), and safe to run any time.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..paths import sqlite_path, vault_root
from .log import log_error
from .transcripts import _BLOCK_RE


def _exchanges_in_window(vault: Path, days: int) -> list[dict[str, str]]:
    """USER→LISAN exchange pairs from the transcript window, in order."""
    exchanges: list[dict[str, str]] = []
    pending: dict[str, str] = {}  # conversation -> awaiting-response user text
    today = date.today()
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        path = vault / "transcripts" / f"{day}.md"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _BLOCK_RE.finditer("\n" + text):
            body = match.group("body").strip()
            conversation = (match.group("conv") or "").strip()
            if body.startswith("USER:"):
                pending[conversation] = body[len("USER:"):].strip()
            elif body.startswith("LISAN:") and conversation in pending:
                user_text = pending.pop(conversation)
                if user_text:
                    exchanges.append(
                        {"conversation_id": conversation, "text": user_text,
                         "response": body[len("LISAN:"):].strip()}
                    )
    return exchanges


def _observed_keys(db_path: Path) -> set[tuple[str, str]]:
    """(conversation_id, user text) for every observe job ever enqueued —
    any status: a queued or failed job is still ownership of the exchange
    (failures retry through the queue, not through this sweep)."""
    keys: set[tuple[str, str]] = set()
    if not Path(db_path).exists():
        return keys
    conn = _db_connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT payload_json FROM jobs WHERE job_type = 'capture.observe'"
            ).fetchall()
        except sqlite3.OperationalError:
            return keys
        for (raw,) in rows:
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            keys.add((str(payload.get("conversation_id") or ""), str(payload.get("text") or "").strip()))
    finally:
        conn.close()
    return keys


def sweep_missed_captures(
    vault: Path | None = None,
    db_path: Path | None = None,
    *,
    days: int = 3,
    enqueue: bool = True,
) -> dict[str, Any]:
    """Diff the transcript window against the observe-job ledger; enqueue
    observe jobs for exchanges that never got one."""
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    exchanges = _exchanges_in_window(vault, days)
    observed = _observed_keys(db_path)
    missed = [
        ex for ex in exchanges
        if (ex["conversation_id"], ex["text"]) not in observed
    ]
    enqueued = 0
    if enqueue and missed:
        from .jobs import enqueue_job

        for ex in missed:
            try:
                enqueue_job(
                    "capture.observe",
                    {
                        "vault": str(vault),
                        "text": ex["text"],
                        "response": ex["response"],
                        "tool_calls": [],
                        "conversation_id": ex["conversation_id"] or None,
                        "retrospective": True,
                    },
                    db_path=db_path,
                )
                enqueued += 1
            except Exception as exc:
                log_error(vault, "retrospective enqueue failed", exc)
    return {"exchanges": len(exchanges), "missed": len(missed), "enqueued": enqueued}
