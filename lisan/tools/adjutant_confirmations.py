"""The confirmation queue: records first, mirror second.

Standing ruling (2026-07-23): the confirmations SQLite table is derived
state — the markdown record is the truth. Every write here goes through
the record and then reindexes it, which is what keeps the mirror synced
between full rebuilds. Approval and denial are themselves captured: the
owner's yes/no is memory too.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path
from .adjutant_common import CONFIRMATION_RESOLUTIONS
from .db import connect as _db_connect
from .record_factory import new_confirmation
from .rebuild_index import ensure_index_schema, reindex_record

DEFAULT_EXPIRY_DAYS = 7


def _conn(db_path: Path | None) -> sqlite3.Connection:
    conn = _db_connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    return conn


def pending_confirmation_for(task_id: str, db_path: Path | None = None) -> str | None:
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM confirmations WHERE task_id = ? AND status = 'pending' AND resolution IS NULL",
            (task_id,),
        ).fetchone()
        return str(row["id"]) if row else None
    finally:
        conn.close()


def create_confirmation_for_task(
    vault: Path,
    *,
    task_id: str,
    task_summary: str,
    planned_action: str,
    risk: str,
    arena: str = "",
    db_path: Path | None = None,
    expires_days: int = DEFAULT_EXPIRY_DAYS,
) -> str | None:
    """Create the pending-approval record, deduped: a task with a live
    pending confirmation never gets a second one (the poller re-selects
    every cycle; the queue must not multiply). Returns the confirmation
    id, or None when an existing one already covers the task."""
    existing = pending_confirmation_for(task_id, db_path)
    if existing:
        return None
    expires = (date.today() + timedelta(days=expires_days)).isoformat()
    created = new_confirmation(
        vault,
        f"confirm {task_id}",
        task_id=task_id,
        task_summary=task_summary,
        planned_action=planned_action,
        risk=risk,
        expires=expires,
        domain_primary=arena or "cross_arena",
    )
    reindex_record(created.path, vault, db_path, quiet=True)
    return str(load_markdown(created.path).frontmatter["id"])


def _resolve(
    vault: Path,
    confirmation_id: str,
    resolution: str,
    *,
    db_path: Path | None = None,
    resolved_by: str = "owner",
    capture: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if resolution not in CONFIRMATION_RESOLUTIONS:
        raise ValueError(f"invalid resolution {resolution!r}")
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"no confirmation {confirmation_id!r}")
    if row["resolution"]:
        raise ValueError(f"{confirmation_id} already resolved: {row['resolution']}")
    record = vault / str(row["record_path"])
    doc = load_markdown(record)
    fm = dict(doc.frontmatter)
    today = date.today().isoformat()
    fm["resolution"] = resolution
    fm["resolved_at"] = today
    fm["resolved_by"] = resolved_by
    fm["updated"] = today
    # Approved stays status=pending: it now awaits execution via the
    # poller's confirmation lane. Denied/expired are terminal.
    fm["status"] = "pending" if resolution == "approved" else "resolved"
    write_markdown(record, fm, doc.body)
    reindex_record(record, vault, db_path, quiet=True)

    # The yes/no is memory too: submit it through the front door.
    if capture is None:
        from .capture import capture_text as capture
    verb = {"approved": "approved", "denied": "denied", "expired": "let expire"}[resolution]
    try:
        capture(
            vault=vault,
            text=(
                f"Owner {verb} confirmation {confirmation_id} for task {fm.get('task_id')}: "
                f"{fm.get('task_summary')}. Planned action was: {fm.get('planned_action')}"
            ),
            conversation_id="adjutant",
            speaker="ADJUTANT",
            db_path=db_path,
        )
    except Exception:
        # Memory of the decision is best-effort; the decision itself is
        # already durable in the record. Never let capture failure undo it.
        pass
    return {"id": confirmation_id, "resolution": resolution, "task_id": str(fm.get("task_id"))}


def approve_confirmation(vault: Path, confirmation_id: str, **kw: Any) -> dict[str, Any]:
    return _resolve(vault, confirmation_id, "approved", **kw)


def deny_confirmation(vault: Path, confirmation_id: str, **kw: Any) -> dict[str, Any]:
    return _resolve(vault, confirmation_id, "denied", **kw)


def mark_executed(vault: Path, task_id: str, db_path: Path | None = None) -> None:
    """After the confirmed task ran, close its approved confirmation."""
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM confirmations WHERE task_id = ? AND resolution = 'approved' AND status = 'pending'",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return
    record = vault / str(row["record_path"])
    doc = load_markdown(record)
    fm = dict(doc.frontmatter)
    fm["status"] = "resolved"
    fm["updated"] = date.today().isoformat()
    write_markdown(record, fm, doc.body)
    reindex_record(record, vault, db_path, quiet=True)


def expire_stale_confirmations(
    vault: Path,
    db_path: Path | None = None,
    *,
    today: str | None = None,
    capture: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Pending confirmations past their expiry become resolution=expired,
    and the originating task moves to task_status=expired so batch review
    surfaces it. Runs at the top of every cycle."""
    today = today or date.today().isoformat()
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, task_id FROM confirmations "
            "WHERE status = 'pending' AND resolution IS NULL AND expires < ?",
            (today,),
        ).fetchall()
        task_paths = {}
        for row in rows:
            task = conn.execute("SELECT path FROM files WHERE id = ?", (str(row["task_id"]),)).fetchone()
            if task:
                task_paths[str(row["task_id"])] = str(task["path"])
    finally:
        conn.close()
    expired: list[dict[str, Any]] = []
    for row in rows:
        outcome = _resolve(
            vault, str(row["id"]), "expired", db_path=db_path, resolved_by="expiry", capture=capture or _noop_capture
        )
        task_path = task_paths.get(str(row["task_id"]))
        if task_path:
            from .adjutant_executor import set_task_status

            try:
                set_task_status(vault, task_path, "expired", db_path)
            except Exception:
                pass
        expired.append(outcome)
    return expired


def _noop_capture(**kw: Any) -> None:
    return None


def list_pending(db_path: Path | None = None, *, today: str | None = None) -> list[dict[str, Any]]:
    today = today or date.today().isoformat()
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, task_id, expires, record_path FROM confirmations "
            "WHERE status = 'pending' AND resolution IS NULL ORDER BY expires",
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


_COMMAND_RE = None


def confirmation_command_response(
    vault: Path,
    text: str,
    db_path: Path | None = None,
    *,
    capture: Callable[..., Any] | None = None,
) -> str | None:
    """Inbound-message hook for the Telegram bot (settled fork: the
    existing bot IS the chat surface). Recognizes:

        /confirmations            -> list pending
        approve <confirmation.id> -> approve (also /approve)
        deny <confirmation.id>    -> deny   (also /deny)

    Returns the reply text, or None when the message is not a
    confirmation command (the bot then treats it as a normal turn)."""
    import re

    global _COMMAND_RE
    if _COMMAND_RE is None:
        _COMMAND_RE = re.compile(r"^/?(approve|deny)\s+(confirmation\.\S+)\s*$", re.IGNORECASE)
    stripped = text.strip()
    if stripped.lower() in ("/confirmations", "/pending"):
        return format_pending(vault, list_pending(db_path))
    match = _COMMAND_RE.match(stripped)
    if not match:
        return None
    action, confirmation_id = match.group(1).lower(), match.group(2)
    try:
        if action == "approve":
            outcome = approve_confirmation(vault, confirmation_id, db_path=db_path, capture=capture)
            return (
                f"Approved {outcome['id']} (task {outcome['task_id']}). "
                "It executes on the next Adjutant cycle — unless intent has since forbidden it."
            )
        outcome = deny_confirmation(vault, confirmation_id, db_path=db_path, capture=capture)
        return f"Denied {outcome['id']} (task {outcome['task_id']})."
    except (KeyError, ValueError) as exc:
        return f"Cannot {action} {confirmation_id}: {exc}"


def format_pending(vault: Path, pending: list[dict[str, Any]]) -> str:
    if not pending:
        return "No pending confirmations."
    lines = []
    for item in pending:
        detail = ""
        try:
            fm = load_markdown(vault / item["record_path"]).frontmatter
            detail = f"\n    will do: {fm.get('planned_action')}\n    risk: {fm.get('risk')}"
        except Exception:
            pass
        lines.append(f"{item['id']}  task={item['task_id']}  expires={item['expires']}{detail}")
    return "\n".join(lines)
