"""The learning loop, step 1: observe.

Lisan acts (tasks, plans, parallel workers) but did not remember *how* it got
things done. This module is the first, deterministic stage of docs/
learning_loop_workorder.md: at each point where work finishes it freezes what
happened into a **learning event**, and it keeps a ledger of which skills were
used and how that went. There is no model call anywhere in here and nothing is
written to any skill; the later stages (a reviewer, a gate, an applier) read
what this one records.

Storage follows the vault's bargain: plain files you own are the truth, and a
SQLite table is an index you can rebuild.

- ``<install>/learning/events/YYYY-MM/events.jsonl``: append-only, one frozen
  event per line, exactly what a later reviewer will be shown. Kept
  indefinitely (owner decision); readers de-duplicate by id.
- ``learning_events`` / ``skill_usage``: the index and the usage ledger.

Events live outside the vault, are never indexed for retrieval, and so cannot
become a side door around the vault's privacy compartments.

Provenance, not permission: every event records the kinds of source its
material came from and a ``tainted`` flag. Nothing here rejects externally
sourced evidence; the flag travels with the event so that policy about how
such material may be used can be written and enforced elsewhere.
"""
from __future__ import annotations

import fcntl
import gzip
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .belief_formation import EVAL_NAMESPACES

MODES = ("off", "observe", "shadow", "auto")
DEFAULT_MODE = "observe"
DEFAULT_MIN_TOOL_CALLS = 5

# Frozen snapshots are what a reviewer reads, but nothing needs an unbounded
# blob: a turn's compacted tool calls are already capped upstream; these cap the
# rest (a plan's step results, a worker's text).
_MAX_TEXT = 4000

KIND_TURN = "turn"
KIND_PLAN = "plan"
KIND_GROUP = "group"
KIND_ADJUTANT = "adjutant"

# Dedicated tools that bring external content in, by name. Mapped to the kind
# of source recorded as provenance.
_SOURCE_BY_TOOL_PREFIX = (
    ("gmail_", "email"),
    ("drive_", "file"),
    ("obsidian_", "file"),
    ("youtube_", "web"),
    ("browser", "web"),
    ("ingest_files", "file"),
    ("research", "web"),
)
# `execute_task` is opaque: a codex run can curl a page or read a mailbox with
# no dedicated tool. Network and mail activity in its args or result counts.
_OPAQUE_TOOLS = {"execute_task", "run_codex"}
_NETWORK_PATTERNS = (
    ("web", re.compile(r"https?://|\b(?:curl|wget)\b", re.I)),
    ("remote_host", re.compile(r"\b(?:ssh|scp|rsync|sftp|telnet)\b", re.I)),
    ("email", re.compile(r"\b(?:sendmail|mutt|imap|smtp)\b", re.I)),
)
# Provenance kinds that are the owner's own doing and so are not taint.
_UNTAINTED_SOURCES = {"owner", "adjutant", "worker"}

_ERROR_PREFIXES = ("error", "traceback", "refused", "no skill named", "i didn't run it")


def learning_root(vault: Path) -> Path:
    """Beside the vault, not inside it (see module docstring)."""
    return Path(vault).parent / "learning"


def learning_mode(config: dict[str, Any] | None) -> str:
    mode = str(((config or {}).get("learning") or {}).get("mode") or DEFAULT_MODE).lower()
    return mode if mode in MODES else DEFAULT_MODE


def _settings(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get("learning") or {}


# Tools that hand a whole job to a multi-step executor. One `run_codex` call is
# one tool call to Lisan and may be an hour of shell work to the executor, so
# counting calls measures the wrong thing for them. Measured on the first real
# history (583 turns): only 5 turns made >= 5 tool calls, yet 65 turns handed
# work to the executor. Configurable as learning.work_tools.
DEFAULT_WORK_TOOLS = ("execute_task", "run_codex")


def work_tools(config: dict[str, Any] | None) -> set[str]:
    configured = _settings(config).get("work_tools")
    if isinstance(configured, (list, tuple)):
        return {str(t) for t in configured}
    return set(DEFAULT_WORK_TOOLS)


def min_tool_calls(config: dict[str, Any] | None) -> int:
    try:
        return max(1, int(_settings(config).get("min_tool_calls", DEFAULT_MIN_TOOL_CALLS)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_TOOL_CALLS


def is_eval_conversation(conversation_id: str | None) -> bool:
    """Beliefs must come from real use, not rehearsals; so must skills. Same
    namespaces the belief extractor excludes."""
    haystack = str(conversation_id or "").lower()
    return any(ns in haystack for ns in EVAL_NAMESPACES)  # the belief extractor's own rule


def _utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def mask_deep(value: Any) -> Any:
    """Mask credentials in every string of a structure before it is frozen. An
    event is kept indefinitely, copied into backups, and later shown to a
    reviewing model; a token the owner once pasted into chat must not ride along.
    The originals stay where they were (the transcripts); only this copy is masked."""
    from ..providers.codex import mask_secrets_strict

    if isinstance(value, str):
        return mask_secrets_strict(value)
    if isinstance(value, list):
        return [mask_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_deep(v) for k, v in value.items()}
    return value


def _clip(text: Any, limit: int = _MAX_TEXT) -> str:
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Provenance ───────────────────────────────────────────────────────────────

def sources_from_text(text: str) -> set[str]:
    """Kinds of source that network/mail activity in `text` implies."""
    return {kind for kind, pattern in _NETWORK_PATTERNS if pattern.search(text or "")}


def sources_from_tool_calls(tool_calls: list[dict[str, Any]]) -> set[str]:
    """Provenance of a turn's material, from tool names and (for the opaque
    executor) the activity visible in its args and result. Never reads content
    for meaning."""
    found: set[str] = set()
    for call in tool_calls or []:
        name = str(call.get("tool") or "")
        for prefix, kind in _SOURCE_BY_TOOL_PREFIX:
            if name.startswith(prefix):
                found.add(kind)
        if name in _OPAQUE_TOOLS:
            blob = json.dumps(call.get("args") or {}, default=str) + " " + str(call.get("result") or "")
            found |= sources_from_text(blob)
    return found


def _provenance(base: set[str], external: set[str]) -> tuple[list[str], bool]:
    sources = sorted(base | external)
    return sources, bool(external - _UNTAINTED_SOURCES)


def _tool_error(tool_calls: list[dict[str, Any]]) -> bool:
    return any(str(c.get("result") or "").strip().lower().startswith(_ERROR_PREFIXES) for c in tool_calls or [])


# ── Skill usage, derived from a turn's tool calls ────────────────────────────

def known_skills(skills_dir: Path | None = None) -> dict[str, str]:
    """name -> 'instructional' | 'executable' for the installed skills."""
    from ..paths import skills_root
    from .skill_loader import load_skills

    try:
        return {
            str(s["name"]): ("executable" if s.get("executable") else "instructional")
            for s in load_skills(skills_dir if skills_dir is not None else skills_root())
        }
    except Exception:
        return {}


def _call_errored(call: dict[str, Any]) -> bool:
    return str(call.get("result") or "").strip().lower().startswith(_ERROR_PREFIXES)


def skills_used(tool_calls: list[dict[str, Any]], known: dict[str, str]) -> list[dict[str, str]]:
    """Skills a turn invoked: `skill(name)` for instructional ones, or the skill
    called directly as a function if it is executable.

    An executable skill's outcome is judged from **its own calls**, not the
    turn: on the first real history that corrected 3 of the 8 turns that looked
    like skill failures (an unrelated tool had errored in the same turn). An
    instructional skill has no call of its own to judge — loading it returns its
    text — so it carries no outcome here and inherits the turn's."""
    used: dict[str, dict[str, str]] = {}
    for call in tool_calls or []:
        tool = str(call.get("tool") or "")
        if tool == "skill":
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            name = str(args.get("name") or "").strip()
            if name:
                used.setdefault(name, {"skill": name, "kind": known.get(name, "instructional")})
        elif tool in known and known[tool] == "executable":
            entry = used.setdefault(tool, {"skill": tool, "kind": "executable", "outcome": "no_error_seen"})
            if _call_errored(call):
                entry["outcome"] = "tool_error"
    return list(used.values())


# ── Storage ──────────────────────────────────────────────────────────────────

_INDEX_DDL = (
    """
    CREATE TABLE IF NOT EXISTS learning_events (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        conversation_id TEXT,
        outcome TEXT,
        tool_call_count INTEGER NOT NULL DEFAULT 0,
        tainted INTEGER NOT NULL DEFAULT 0,
        sources TEXT,
        skills_used TEXT,
        summary TEXT,
        file TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_learning_events_time ON learning_events(occurred_at)",
    """
    CREATE TABLE IF NOT EXISTS skill_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill TEXT NOT NULL,
        skill_kind TEXT,
        version TEXT,
        event_id TEXT NOT NULL,
        used_at TEXT NOT NULL,
        outcome TEXT,
        UNIQUE (skill, event_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill, used_at)",
)


def ensure_learning_tables(conn: sqlite3.Connection) -> None:
    for statement in _INDEX_DDL:
        conn.execute(statement)


def _connect(db_path: Path | None):
    from .db import connect

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_learning_tables(conn)
    return conn


def _events_file(root: Path, occurred_at: str) -> Path:
    month = (occurred_at or _utc())[:7]
    return root / "events" / month / "events.jsonl"


def _event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    return conn.execute("SELECT 1 FROM learning_events WHERE id = ?", (event_id,)).fetchone() is not None


def _append_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _index_row(conn: sqlite3.Connection, event: dict[str, Any], file: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO learning_events (id, kind, occurred_at, recorded_at, conversation_id, outcome, "
        "tool_call_count, tainted, sources, skills_used, summary, file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event["id"], event["kind"], event["occurred_at"], event["recorded_at"], event.get("conversation_id"),
            event.get("outcome"), int(event.get("tool_call_count") or 0), 1 if event.get("tainted") else 0,
            json.dumps(event.get("sources") or []), json.dumps(event.get("skills_used") or []),
            _clip(event.get("summary"), 300), file,
        ),
    )


def _record_usage(conn: sqlite3.Connection, event: dict[str, Any], skills_dir: Path | None) -> None:
    from ..paths import skills_root

    root = skills_dir if skills_dir is not None else skills_root()
    for used in event.get("skills_used") or []:
        version = None
        try:
            from .skill_loader import load_skill_manifest

            manifest = load_skill_manifest(root, used["skill"])
            version = str((manifest or {}).get("version") or "") or None
        except Exception:
            pass
        conn.execute(
            "INSERT OR IGNORE INTO skill_usage (skill, skill_kind, version, event_id, used_at, outcome) VALUES (?,?,?,?,?,?)",
            (used["skill"], used.get("kind"), version, event["id"], event["occurred_at"],
             used.get("outcome") or event.get("outcome")),
        )


def record_event(
    event: dict[str, Any],
    *,
    vault: Path,
    db_path: Path | None,
    skills_dir: Path | None = None,
) -> bool:
    """Persist one frozen event. Idempotent by id: returns False if it was
    already recorded. The file is the truth, so it is written first; a crash
    before the index row is repaired by `rebuild_index`, and readers
    de-duplicate by id."""
    conn = _connect(db_path)
    try:
        if _event_exists(conn, event["id"]):
            return False
        path = _events_file(learning_root(vault), event["occurred_at"])
        _append_line(path, event)
        _index_row(conn, event, str(path))
        _record_usage(conn, event, skills_dir)
        conn.commit()
        return True
    finally:
        conn.close()


def _base_event(
    event_id: str, kind: str, *, occurred_at: str | None, conversation_id: str | None, outcome: str,
    summary: str, tool_calls: list[dict[str, Any]], skills: list[dict[str, str]], sources: list[str],
    tainted: bool, payload: dict[str, Any], refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "kind": kind,
        "recorded_at": _utc(),
        "occurred_at": occurred_at or _utc(),
        "conversation_id": conversation_id,
        "outcome": outcome,
        "summary": mask_deep(_clip(summary, 300)),
        "tool_call_count": len(tool_calls),
        "tools": sorted({str(c.get("tool") or "") for c in tool_calls}),
        "skills_used": skills,
        "sources": sources,
        "tainted": tainted,
        "payload": mask_deep(payload),
        "refs": refs or {},
    }


# ── Recording: one function per place work finishes ──────────────────────────
# Each returns the event id if it recorded one, else None. None of them may
# raise into the work they observe: the caller wraps them, and they are also
# written to be safe when learning is off.

def record_turn_event(
    payload: dict[str, Any],
    *,
    job_id: str,
    occurred_at: str | None = None,
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None = None,
    skills_dir: Path | None = None,
) -> str | None:
    """A finished conversation turn (from its `capture.observe` payload).

    Recorded when it made >= `learning.min_tool_calls` tool calls, used a
    skill, or handed work to the executor (`learning.work_tools`). A turn inside
    a plan is not an event of its own (the plan's event covers it) but its skill
    use still goes in the usage ledger.
    """
    if learning_mode(config) == "off":
        return None
    conversation_id = str(payload.get("conversation_id") or "") or None
    if is_eval_conversation(conversation_id):
        return None
    # the full-length record when the turn carried one (newer turns), else the
    # compact form every older capture.observe payload has
    raw_calls = payload.get("tool_calls_full") or payload.get("tool_calls") or []
    calls = [c for c in raw_calls if isinstance(c, dict)]
    used = skills_used(calls, known_skills(skills_dir))
    outcome = "tool_error" if _tool_error(calls) else "no_error_seen"
    external = sources_from_tool_calls(calls)

    if str(conversation_id or "").startswith("plan-"):
        if used:  # usage only: the plan's own event is the learning event
            conn = _connect(db_path)
            try:
                _record_usage(
                    conn,
                    {"id": f"plan-turn:{job_id}", "occurred_at": occurred_at or _utc(), "outcome": outcome, "skills_used": used},
                    skills_dir,
                )
                conn.commit()
            finally:
                conn.close()
        return None
    handed_off = any(str(c.get("tool") or "") in work_tools(config) for c in calls)
    if len(calls) < min_tool_calls(config) and not used and not handed_off:
        return None

    sources, tainted = _provenance({"owner"}, external)
    event = _base_event(
        f"turn:{job_id}", KIND_TURN, occurred_at=occurred_at, conversation_id=conversation_id, outcome=outcome,
        summary=" ".join(str(payload.get("text") or "").split())[:300], tool_calls=calls, skills=used,
        sources=sources, tainted=tainted,
        payload={
            "text": _clip(payload.get("text")), "response": _clip(payload.get("response")),
            "tool_calls": calls,
            # honest about what the reviewer is NOT seeing: the compact form keeps
            # 10 calls of 1500 chars; the full form 40 of 20000
            "tool_calls_truncated_upstream": len(calls) >= (40 if payload.get("tool_calls_full") else 10),
        },
        refs={"job_id": job_id},
    )
    return event["id"] if record_event(event, vault=vault, db_path=db_path, skills_dir=skills_dir) else None


def _plan_status(plan: dict[str, Any], status: str) -> str:
    return {"completed": "succeeded", "failed": "failed", "canceled": "canceled"}.get(status, status)


def record_plan_event(
    plan: dict[str, Any],
    status: str,
    *,
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None = None,
    occurred_at: str | None = None,
) -> str | None:
    """A plan that finished (completed, failed, or canceled)."""
    if learning_mode(config) == "off":
        return None
    conversation_id = str(plan.get("conversation_id") or "") or None
    if is_eval_conversation(conversation_id):
        return None
    steps = [s for s in plan.get("steps") or [] if isinstance(s, dict)]
    blob = " ".join(str(s.get("description") or "") + " " + str(s.get("result") or "") for s in steps)
    sources, tainted = _provenance({"owner"}, sources_from_text(blob))
    plan_id = str(plan.get("plan_id") or "")
    event = _base_event(
        f"plan:{plan_id}:r{int(plan.get('resume_count') or 0)}:{status}", KIND_PLAN, occurred_at=occurred_at,
        conversation_id=conversation_id, outcome=_plan_status(plan, status), summary=str(plan.get("goal") or ""),
        tool_calls=[{"tool": f"plan_step:{s.get('kind')}"} for s in steps], skills=[], sources=sources, tainted=tainted,
        payload={
            "goal": plan.get("goal"), "resume_count": int(plan.get("resume_count") or 0),
            "steps": [
                {
                    "kind": s.get("kind"), "description": _clip(s.get("description"), 600), "status": s.get("status"),
                    "attempts": s.get("attempts"), "result": _clip(s.get("result"), 2000),
                    **({"children": [_clip(c.get("brief"), 400) for c in s.get("children") or []], "join": s.get("join")}
                       if s.get("kind") == "fanout" else {}),
                }
                for s in steps
            ],
        },
        refs={"plan_id": plan_id},
    )
    return event["id"] if record_event(event, vault=vault, db_path=db_path) else None


def record_group_event(
    group_id: str,
    goal: str,
    children: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None = None,
) -> str | None:
    """A set of delegated workers started from chat, all finished."""
    if learning_mode(config) == "off" or is_eval_conversation(conversation_id):
        return None
    failed = [c for c in children if c.get("status") != "succeeded"]
    blob = " ".join(str(c.get("brief") or "") + " " + str(c.get("text") or "") for c in children)
    sources, tainted = _provenance({"owner", "worker"}, sources_from_text(blob))
    event = _base_event(
        f"group:{group_id}", KIND_GROUP, occurred_at=None, conversation_id=conversation_id,
        outcome="failed" if failed else "succeeded", summary=goal or "delegated work",
        tool_calls=[{"tool": "delegate"} for _ in children], skills=[], sources=sources, tainted=tainted,
        payload={
            "goal": goal,
            "children": [
                {
                    "brief": _clip(c.get("brief"), 1500), "profile": c.get("profile"), "status": c.get("status"),
                    "text": _clip(c.get("text"), 2000), "error": _clip(c.get("error"), 500),
                    "duration_s": c.get("duration_s"),
                }
                for c in children
            ],
        },
        refs={"group_id": group_id},
    )
    return event["id"] if record_event(event, vault=vault, db_path=db_path) else None


def record_adjutant_event(
    *,
    task_id: str,
    attempt: int,
    kinds: list[str],
    summary: str,
    ok: bool,
    actions: list[str],
    errors: list[str],
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None = None,
) -> str | None:
    """An Adjutant task attempt that ran (success or failure)."""
    if learning_mode(config) == "off":
        return None
    external = sources_from_text(" ".join(actions + errors))
    if "research" in kinds:
        external.add("web")
    sources, tainted = _provenance({"adjutant"}, external)
    event = _base_event(
        f"adjutant:{task_id}:a{attempt}", KIND_ADJUTANT, occurred_at=None, conversation_id="adjutant",
        outcome="succeeded" if ok else "failed", summary=summary or task_id,
        tool_calls=[{"tool": f"adjutant:{k}"} for k in kinds], skills=[], sources=sources, tainted=tainted,
        payload={"task_id": task_id, "kinds": kinds, "attempt": attempt,
                 "actions": [_clip(a, 400) for a in actions[:20]], "errors": [_clip(e, 600) for e in errors[:10]]},
        refs={"task_id": task_id},
    )
    return event["id"] if record_event(event, vault=vault, db_path=db_path) else None


# ── Reading ──────────────────────────────────────────────────────────────────

def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn final line from a crash; the rest is intact
                if isinstance(record, dict) and record.get("id"):
                    yield record
    except OSError:
        return


def iter_event_files(vault: Path) -> list[Path]:
    root = learning_root(vault) / "events"
    if not root.exists():
        return []
    return sorted([*root.glob("*/events.jsonl"), *root.glob("*/events.jsonl.gz")])


def iter_events(vault: Path) -> Iterator[dict[str, Any]]:
    """Every recorded event, oldest file first, each id once."""
    seen: set[str] = set()
    for path in iter_event_files(vault):
        for record in _read_lines(path):
            if record["id"] not in seen:
                seen.add(record["id"])
                yield record


def get_event(event_id: str, *, vault: Path) -> dict[str, Any] | None:
    for record in iter_events(vault):
        if record["id"] == event_id:
            return record
    return None


def list_events(*, db_path: Path | None, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        sql = "SELECT * FROM learning_events"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def rebuild_index(vault: Path, db_path: Path | None, skills_dir: Path | None = None) -> dict[str, int]:
    """Rebuild the index and the usage ledger from the event files. The files
    are the truth; this is what makes the table disposable. Usage rows are
    re-derived from each event's recorded skills; outcomes come from the event."""
    conn = _connect(db_path)
    events = usage = 0
    try:
        conn.execute("DELETE FROM learning_events")
        conn.execute("DELETE FROM skill_usage")
        for path in iter_event_files(vault):
            for record in _read_lines(path):
                if _event_exists(conn, record["id"]):
                    continue
                _index_row(conn, record, str(path))
                _record_usage(conn, record, skills_dir)
                events += 1
        usage = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"events": events, "usage_rows": int(usage)}


# ── Status and the skill ledger ──────────────────────────────────────────────

def skill_usage_summary(db_path: Path | None, *, days: int | None = 30) -> list[dict[str, Any]]:
    """Per skill: uses, outcomes, last used. Outcomes are honest about what is
    known: a chat turn is `no_error_seen`, not `succeeded`."""
    conn = _connect(db_path)
    try:
        sql = "SELECT skill, skill_kind, outcome, used_at FROM skill_usage"
        params: list[Any] = []
        if days:
            sql += " WHERE used_at >= ?"
            params.append(_utc(time.time() - days * 86400))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        s = stats.setdefault(row["skill"], {"skill": row["skill"], "kind": row["skill_kind"], "uses": 0, "outcomes": {}, "last_used": ""})
        s["uses"] += 1
        outcome = row["outcome"] or "unknown"
        s["outcomes"][outcome] = s["outcomes"].get(outcome, 0) + 1
        s["last_used"] = max(s["last_used"], row["used_at"])
    return sorted(stats.values(), key=lambda s: (-s["uses"], s["skill"]))


def learning_status(vault: Path, db_path: Path | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM learning_events").fetchone()[0]
        by_kind = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) n FROM learning_events GROUP BY kind")}
        tainted = conn.execute("SELECT COUNT(*) FROM learning_events WHERE tainted = 1").fetchone()[0]
        last = conn.execute("SELECT MAX(occurred_at) FROM learning_events").fetchone()[0]
    finally:
        conn.close()
    size = sum(p.stat().st_size for p in iter_event_files(vault))
    return {
        "mode": learning_mode(config),
        "min_tool_calls": min_tool_calls(config),
        "work_tools": sorted(work_tools(config)),
        "events": int(total),
        "by_kind": by_kind,
        "tainted": int(tainted),
        "last_event": last,
        "files": len(iter_event_files(vault)),
        "bytes": int(size),
        "root": str(learning_root(vault)),
    }


def format_status(status: dict[str, Any]) -> str:
    lines = [
        f"Learning: mode {status['mode']} — {status['events']} event(s) recorded"
        + (f", last {status['last_event']}" if status.get("last_event") else ""),
    ]
    if status["events"]:
        kinds = ", ".join(f"{k}×{n}" for k, n in sorted(status["by_kind"].items()))
        lines.append(f"  by kind: {kinds}; {status['tainted']} drew on external sources (provenance, not a gate)")
        lines.append(f"  stored in {status['files']} file(s), {status['bytes']:,} bytes under {status['root']}")
    lines.append(
        f"  a conversation turn is recorded at ≥ {status['min_tool_calls']} tool calls, when it uses a skill, "
        f"or when it hands work to the executor ({', '.join(sorted(status['work_tools'])) or 'none'})"
    )
    return "\n".join(lines)


# ── Backfill from history ────────────────────────────────────────────────────

def backfill(
    vault: Path,
    db_path: Path | None,
    config: dict[str, Any] | None = None,
    *,
    since: str | None = None,
    skills_dir: Path | None = None,
) -> dict[str, int]:
    """Record events for work that finished before learning was switched on.

    Reads finished `capture.observe` jobs (their payloads carry the turn and its
    tool calls) and finished plans, applying exactly the same rules as live
    recording. Idempotent: ids are derived from the source, so running it twice
    records nothing new. `since` is a YYYY-MM-DD lower bound."""
    from .jobs import list_jobs

    counts = {"turns_seen": 0, "turn_events": 0, "plans_seen": 0, "plan_events": 0}
    for job in list_jobs(limit=1_000_000, db_path=db_path):
        if job.get("job_type") != "capture.observe" or job.get("status") != "succeeded":
            continue
        finished = str(job.get("finished_at") or job.get("created_at") or "")
        if since and finished[:10] < since:
            continue
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        counts["turns_seen"] += 1
        if record_turn_event(
            payload, job_id=str(job["id"]), occurred_at=finished or None, vault=vault, db_path=db_path,
            config=config, skills_dir=skills_dir,
        ):
            counts["turn_events"] += 1

    from .plans import list_plans

    from .jobs import get_job

    for plan in list_plans(db_path=db_path, limit=100_000):
        job = get_job(str(plan["job_id"]), db_path=db_path) or {}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        steps = payload.get("steps") or []
        statuses = {s.get("status") for s in steps}
        if "failed" in statuses:
            status = "failed"
        elif steps and statuses == {"done"}:
            status = "completed"
        else:
            continue  # still running, or canceled mid-way: not a finished piece of work
        finished = str(job.get("finished_at") or job.get("created_at") or "")
        if since and finished[:10] < since:
            continue
        counts["plans_seen"] += 1
        if record_plan_event(payload, status, vault=vault, db_path=db_path, config=config, occurred_at=finished or None):
            counts["plan_events"] += 1
    return counts
