"""Applying a gated change to a skill on disk — the last, careful step.

The gate decided a change was allowed and planned exactly what to write. This
module writes it, and does so as if it might be wrong:

 - **snapshot first** (the owner's own text becomes `v0` the first time), so any
   change can be undone with one command and nothing else;
 - **refuse stale plans**: a change was planned against the text the gate saw; if
   that text has since changed (the owner edited it, another review wrote it) the
   plan is no longer about this file and is dropped, never merged;
 - **respect a pin placed after planning**;
 - **write atomically** (temp file then rename; a new skill appears whole or not
   at all), so a crash cannot leave half a skill;
 - **verify the result** loads as a valid skill, and undo the write if it does not;
 - **log** what changed, why, on what evidence, from which sources.

Nothing here decides *whether* a change is allowed; that is the gate's job.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skill_format import SKILL_FILE, parse_skill_md
from .skill_gate import PlannedChange
from .skill_history import HISTORY_DIR, SkillHistoryError, _log, is_pinned, rollback_skill, snapshot_skill


class ApplyRefused(RuntimeError):
    """The change was not written, and why. Never a partial write."""


@dataclass
class Applied:
    skill: str
    op: str
    snapshot: str | None  # the version id to roll back to; None for a new skill
    files: list[str] = field(default_factory=list)
    version_before: str | None = None
    version_after: str | None = None


class _Lock:
    """One writer at a time per skills directory (two reviews, or a review and the
    lifecycle pass, must not interleave their snapshots and writes)."""

    def __init__(self, skills_dir: Path) -> None:
        folder = skills_dir / HISTORY_DIR
        folder.mkdir(parents=True, exist_ok=True)
        self._handle = (folder / "apply.lock").open("a")

    def __enter__(self) -> "_Lock":
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        self._handle.close()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".apply-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _valid(skill_dir: Path) -> str | None:
    """None if the skill on disk is a valid skill, else why not."""
    md = skill_dir / SKILL_FILE
    if not md.is_file():
        return "SKILL.md is missing"
    manifest = parse_skill_md(md)
    if manifest.errors:
        return "; ".join(str(e) for e in manifest.errors)
    return None


def apply_change(
    change: PlannedChange,
    *,
    skills_dir: Path,
    review_id: str,
    actor: str = "reviewer",
    reason: str = "",
    events: list[str] | None = None,
) -> Applied:
    """Write one planned change. Raises `ApplyRefused` (and leaves the skill as it
    was) if it cannot be done safely."""
    target = skills_dir / change.skill
    who = f"{actor}:{review_id}"
    with _Lock(skills_dir):
        if change.is_new:
            if target.exists():
                raise ApplyRefused(f"{change.skill!r} now exists; the plan was for a new skill")
        else:
            if not target.is_dir():
                raise ApplyRefused(f"{change.skill!r} no longer exists")
            if is_pinned(skills_dir, change.skill):
                raise ApplyRefused(f"{change.skill!r} was pinned after this change was planned")
            for rel, planned_against in change.old_files.items():
                path = target / rel
                current = path.read_text(encoding="utf-8") if path.is_file() else ""
                if current != planned_against:
                    raise ApplyRefused(
                        f"{change.skill}/{rel} changed after this change was planned; the plan was for text that "
                        "is no longer there and will not be merged blindly"
                    )

        snapshot: str | None = None
        if not change.is_new:
            try:
                snapshot = snapshot_skill(
                    skills_dir, change.skill, reason=reason or f"before {change.op} by {who}", actor=who,
                    events=events or change.provenance.get("source_events"), action="snapshot",
                )
            except SkillHistoryError as exc:
                raise ApplyRefused(f"could not snapshot {change.skill!r}: {exc}") from exc

        written: list[str] = []
        try:
            if change.is_new:
                staging = Path(tempfile.mkdtemp(prefix=".new-skill-", dir=skills_dir))
                try:
                    for rel, text in change.files.items():
                        _atomic_write(staging / rel, text)
                    problem = _valid(staging)
                    if problem:
                        raise ApplyRefused(f"the new skill would not be valid: {problem}")
                    staging.rename(target)  # the whole skill appears at once
                    staging = None  # type: ignore[assignment]
                finally:
                    if staging is not None:
                        shutil.rmtree(staging, ignore_errors=True)
                written = sorted(change.files)
            else:
                for rel, text in change.files.items():
                    _atomic_write(target / rel, text)
                    written.append(rel)
                problem = _valid(target)
                if problem:
                    raise ApplyRefused(f"the result would not be a valid skill: {problem}")
        except BaseException:
            # undo: restore the snapshot (or remove what we created) so a failed
            # apply leaves the skill exactly as it was
            if change.is_new:
                shutil.rmtree(target, ignore_errors=True)
            elif snapshot:
                try:
                    rollback_skill(skills_dir, change.skill, snapshot, actor=who, reason="apply failed; restored")
                except Exception:
                    pass
            raise

        _log(skills_dir, {
            "skill": change.skill, "action": "apply", "actor": who, "op": change.op,
            "reason": reason or f"{change.op} by {who}", "events": events or change.provenance.get("source_events") or [],
            "snapshot": snapshot, "files": written, "version": [change.version_before, change.version_after],
            "sources": change.provenance.get("sources"), "tainted": change.provenance.get("tainted"),
            "review_id": review_id,
        })
    return Applied(
        skill=change.skill, op=change.op, snapshot=snapshot, files=written,
        version_before=change.version_before, version_after=change.version_after,
    )
