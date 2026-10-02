"""Probation: a skill the loop wrote earns trust by being used.

A new agent-written skill starts `provisional`. It becomes `established` after it
has been used and has held up (at least `PROMOTE_USES` uses that raised no error,
no recent failure, and at least `PROMOTE_MIN_DAYS` old), or when the owner says
so. If it fails twice in a row it is `flagged` for review — never deleted: a
failing skill may be right and the environment wrong, which is exactly the case
that must not be silently buried. A revision to a flagged skill puts it back on
probation.

Only skills the loop made are touched. The agent is told a skill's standing
(`status_note`), because it is an instrument reading, not a prompt asking for care.
"""
from __future__ import annotations

import calendar
import time
from pathlib import Path
from typing import Any

from . import learning
from . import skill_frontmatter as fm
from .skill_apply import _atomic_write, _Lock
from .skill_format import SKILL_FILE
from .skill_history import SkillHistoryError, _log, read_provenance, snapshot_skill

PROMOTE_USES = 3
PROMOTE_MIN_DAYS = 2
FLAG_AFTER_CONSECUTIVE_FAILURES = 2
OK_OUTCOMES = {"no_error_seen", "succeeded"}


def _age_days(created: Any, now: float) -> float | None:
    try:
        then = calendar.timegm(time.strptime(str(created)[:10], "%Y-%m-%d"))
    except (ValueError, TypeError):
        return None
    return (now - then) / 86400


def _set_status(skills_dir: Path, name: str, status: str, *, action: str, reason: str, actor: str) -> None:
    """Change a skill's status, with a snapshot first and a log entry after."""
    with _Lock(skills_dir):
        snapshot_skill(skills_dir, name, reason=reason, actor=actor, action="snapshot")
        path = skills_dir / name / SKILL_FILE
        _atomic_write(path, fm.set_metadata(path.read_text(encoding="utf-8"), {"status": status}))
        _log(skills_dir, {"skill": name, "action": action, "actor": actor, "reason": reason, "status": status})


def decide(prov: dict[str, Any], uses: list[tuple[str, str]], now: float) -> tuple[str, str] | None:
    """(new status, reason) for one skill, or None to leave it. Pure, so it can be
    tested without a filesystem."""
    status = prov.get("status")
    tail = 0
    for _, outcome in reversed(uses):
        if outcome == "tool_error":
            tail += 1
        else:
            break
    if status in ("provisional", "established", None) and tail >= FLAG_AFTER_CONSECUTIVE_FAILURES:
        return "flagged", f"its last {tail} uses failed"
    if status == "provisional":
        ok = sum(1 for _, outcome in uses if outcome in OK_OUTCOMES)
        age = _age_days(prov.get("created"), now)
        if ok >= PROMOTE_USES and tail == 0 and (age is None or age >= PROMOTE_MIN_DAYS):
            return "established", f"used {ok} times without error"
    return None


def evaluate_lifecycle(
    skills_dir: Path, db_path: Path | None, *, apply: bool = True, now: float | None = None
) -> list[dict[str, Any]]:
    """Promote or flag the loop's own skills from their recorded use. Returns what
    changed (or would, with apply=False)."""
    now = time.time() if now is None else now
    changes: list[dict[str, Any]] = []
    if not skills_dir.is_dir():
        return changes
    for path in sorted(p for p in skills_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        if not (path / SKILL_FILE).is_file():
            continue
        prov = read_provenance(skills_dir, path.name)
        if prov["origin"] != "agent" or prov.get("pinned"):
            continue
        verdict = decide(prov, learning.skill_usage_rows(db_path, path.name), now)
        if verdict is None or verdict[0] == prov.get("status"):
            continue
        status, reason = verdict
        changes.append({"skill": path.name, "from": prov.get("status"), "to": status, "reason": reason})
        if apply:
            _set_status(
                skills_dir, path.name, status, action="promote" if status == "established" else "flag",
                reason=reason, actor="lifecycle",
            )
    return changes


def approve_skill(skills_dir: Path, name: str, *, actor: str = "owner") -> str:
    """The owner vouches for a skill: it is established, whatever its record."""
    prov = read_provenance(skills_dir, name)
    if prov["origin"] != "agent":
        raise SkillHistoryError(f"{name!r} is yours, not the agent's; there is nothing to approve")
    if prov.get("status") == "established":
        return "already established"
    _set_status(skills_dir, name, "established", action="approve", reason="approved by the owner", actor=actor)
    return "established"


def status_note(skills_dir: Path, name: str) -> str:
    """What the agent should know about a skill's standing before it follows it."""
    try:
        prov = read_provenance(skills_dir, name)
    except SkillHistoryError:
        return ""
    if prov["origin"] != "agent":
        return ""
    if prov.get("status") == "flagged":
        return ("[This skill was written by the agent from past work, and its recent uses have failed. Treat it with "
                "suspicion: verify each step before relying on it, and say so if it does not hold.]\n\n")
    if prov.get("status") == "provisional":
        return ("[This skill was written by the agent from past work and has not yet proven itself. Follow it, but check "
                "each step against the situation in front of you.]\n\n")
    return ""


def catalogue_suffix(skills_dir: Path, name: str) -> str:
    """A short tag for the skill list the agent reads when choosing a skill."""
    try:
        prov = read_provenance(skills_dir, name)
    except SkillHistoryError:
        return ""
    if prov["origin"] != "agent":
        return ""
    return {"provisional": " [provisional, agent-written]", "flagged": " [flagged: recent uses failed]"}.get(prov.get("status") or "", "")
