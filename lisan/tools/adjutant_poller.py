"""The Adjutant poller: what is actionable right now? Pure SQL + code.

Selects from the existing index, never from LLM judgment:
1. open_loops: active, tasked, pending, and (execute_asap or due).
2. decisions: active with a pending execution step (the indexer surfaces
   this as files.task_status='pending').
3. schedules: active with next_run now-or-past.
4. approved confirmations awaiting execution.

Ordering: approved confirmations first, then intent-priority match, then
due date, then created date. Capped by intent's max_tasks_per_cycle.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, load_markdown
from .intent import Intent


@dataclass(slots=True)
class PolledTask:
    task_id: str
    source: str          # open_loop | decision | schedule | confirmation
    arena: str
    task_kinds: list[str]
    path: str
    summary: str
    due: str = ""
    created: str = ""
    priority_rank: int = 10**6
    blocked_contexts: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def as_gate_task(self) -> dict[str, Any]:
        return {
            "arena": self.arena,
            "task_kinds": self.task_kinds,
            "blocked_contexts": self.blocked_contexts,
        }


_PRIORITY_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s+|-\s+)(.*\S)\s*$")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]{3,}")


def intent_priorities(intent: Intent) -> list[str]:
    lines = []
    for line in intent.sections.get("Priorities", "").splitlines():
        match = _PRIORITY_LINE_RE.match(line)
        if match:
            lines.append(match.group(1).lower())
    return lines


def priority_rank(text: str, priorities: list[str]) -> int:
    """Index of the first intent priority sharing a substantive word with
    the task; unmatched tasks rank after every matched one. Deterministic
    token overlap — a heuristic the owner steers by writing priorities in
    the task's own vocabulary, not a model's opinion."""
    words = set(_WORD_RE.findall(text.lower()))
    for index, line in enumerate(priorities):
        if words & set(_WORD_RE.findall(line)):
            return index
    return 10**6


def _disabled_arenas(intent: Intent) -> set[str]:
    arenas = intent.delegations.get("arenas", {}) or {}
    disabled = {
        name for name, rules in arenas.items()
        if isinstance(rules, dict) and rules.get("mode") == "disabled"
    }
    if (intent.delegations.get("defaults", {}) or {}).get("mode") == "disabled":
        disabled.add("*")
    return disabled


def _arena_is_disabled(arena: str, disabled: set[str], intent: Intent) -> bool:
    if arena in disabled:
        return True
    if "*" in disabled:
        listed = (intent.delegations.get("arenas", {}) or {}).keys()
        return arena not in listed
    return False


def poll(
    conn: sqlite3.Connection,
    intent: Intent,
    vault: Path,
    *,
    today: str | None = None,
    now: str | None = None,
) -> list[PolledTask]:
    today = today or date.today().isoformat()
    now = now or datetime.now().isoformat(timespec="seconds")
    priorities = intent_priorities(intent)
    disabled = _disabled_arenas(intent)
    tasks: list[PolledTask] = []

    # 4 first: approved confirmations jump the queue by construction.
    confirmed: list[PolledTask] = []
    for row in conn.execute(
        "SELECT c.id, c.task_id, f.arena, f.task_kind, f.path, f.summary, f.created, f.blocked_contexts "
        "FROM confirmations c LEFT JOIN files f ON f.id = c.task_id "
        "WHERE c.resolution = 'approved'"
    ):
        confirmed.append(
            PolledTask(
                task_id=str(row["task_id"]),
                source="confirmation",
                arena=str(row["arena"] or ""),
                task_kinds=[str(row["task_kind"])] if row["task_kind"] else [],
                path=str(row["path"] or ""),
                summary=str(row["summary"] or ""),
                created=str(row["created"] or ""),
                priority_rank=-1,
                blocked_contexts=_json_list(row["blocked_contexts"]),
            )
        )

    for row in conn.execute(
        "SELECT id, arena, task_kind, path, summary, created, due, blocked_contexts, execute_asap "
        "FROM files WHERE type = 'open_loop' AND status = 'active' AND task_kind IS NOT NULL "
        "AND task_status = 'pending' AND (execute_asap = 1 OR (due IS NOT NULL AND due != '' AND due <= ?))",
        (today,),
    ):
        tasks.append(
            PolledTask(
                task_id=str(row["id"]),
                source="open_loop",
                arena=str(row["arena"] or ""),
                task_kinds=[str(row["task_kind"])],
                path=str(row["path"]),
                summary=str(row["summary"] or ""),
                due=str(row["due"] or ""),
                created=str(row["created"] or ""),
                blocked_contexts=_json_list(row["blocked_contexts"]),
            )
        )

    for row in conn.execute(
        "SELECT id, arena, path, summary, created, blocked_contexts FROM files "
        "WHERE type = 'decision' AND status = 'active' AND task_status = 'pending'"
    ):
        kinds = _pending_step_kinds(vault / str(row["path"]))
        if not kinds:
            continue
        tasks.append(
            PolledTask(
                task_id=str(row["id"]),
                source="decision",
                arena=str(row["arena"] or ""),
                task_kinds=kinds,
                path=str(row["path"]),
                summary=str(row["summary"] or ""),
                created=str(row["created"] or ""),
                blocked_contexts=_json_list(row["blocked_contexts"]),
            )
        )

    for row in conn.execute(
        "SELECT id, arena, task_kind, path, summary, created, next_run, blocked_contexts FROM files "
        "WHERE type = 'schedule' AND status = 'active' AND next_run IS NOT NULL AND next_run != '' AND next_run <= ?",
        (now,),
    ):
        tasks.append(
            PolledTask(
                task_id=str(row["id"]),
                source="schedule",
                arena=str(row["arena"] or ""),
                task_kinds=[str(row["task_kind"])] if row["task_kind"] else [],
                path=str(row["path"]),
                summary=str(row["summary"] or ""),
                due=str(row["next_run"] or ""),
                created=str(row["created"] or ""),
                blocked_contexts=_json_list(row["blocked_contexts"]),
            )
        )

    # Disabled arenas are never selected — not even to be denied. The
    # negative test in the spec pins this.
    tasks = [t for t in tasks if not _arena_is_disabled(t.arena, disabled, intent)]
    confirmed = [t for t in confirmed if not _arena_is_disabled(t.arena, disabled, intent)]

    # A task with an approved confirmation rides the confirmation lane only;
    # re-selecting it as a pending loop would double-issue its verdict.
    confirmed_ids = {t.task_id for t in confirmed}
    tasks = [t for t in tasks if t.task_id not in confirmed_ids]

    # Rank on the summary only: arena names are often everyday words
    # ("work", "financial") and would false-match priority prose.
    for task in tasks:
        task.priority_rank = priority_rank(task.summary, priorities)
    tasks.sort(key=lambda t: (t.priority_rank, t.due or "9999-12-31", t.created))

    ordered = confirmed + tasks
    cap = int((intent.delegations.get("global", {}) or {}).get("max_tasks_per_cycle", 5) or 5)
    return ordered[:cap]


def _pending_step_kinds(path: Path) -> list[str]:
    """Kinds of a decision's pending steps, read deterministically from the
    record. Malformed records contribute nothing — the validator owns
    complaining about them."""
    try:
        doc = load_markdown(path)
    except (FrontmatterError, OSError):
        return []
    steps = doc.frontmatter.get("execution_steps") or []
    kinds: list[str] = []
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("status") == "pending":
                kind = str(step.get("task_kind", ""))
                if kind and kind not in kinds:
                    kinds.append(kind)
    return kinds


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []
