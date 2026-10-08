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
import hashlib
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skill_format import SKILL_FILE, parse_frontmatter, parse_skill_md
from .skill_gate import PlannedChange, check_name, scan_added_text
from .skill_history import HISTORY_DIR, SkillHistoryError, _log, is_pinned, rollback_skill, snapshot_skill
from . import skill_frontmatter as fm


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


def create_requested_skill(
    *, name: str, description: str, body: str, skills_dir: Path,
) -> Applied:
    """Create one provisional, instruction-only skill through the audited writer.

    The model supplies content, never a path or extra files. External or otherwise
    unverified source material is conservatively marked tainted and the skill is
    provisional until its use is reviewed.
    """
    name = str(name or "").strip()
    description = str(description or "").strip()
    body = str(body or "").strip()
    if not body:
        raise ApplyRefused("skill body must not be empty")
    if len(description) > 1024 or "\n" in description:
        raise ApplyRefused("description must be one line and at most 1024 characters")
    if not description.lower().startswith("use when "):
        raise ApplyRefused("description must begin with 'Use when' and state when the skill applies")
    if len(body.encode("utf-8")) > 16_000:
        raise ApplyRefused("skill body exceeds the 16,000-byte limit")
    problems = check_name(name)
    if problems:
        raise ApplyRefused("; ".join(problems))
    problems = scan_added_text(body)
    if problems:
        raise ApplyRefused("skill content was refused: " + "; ".join(problems))

    text = (
        f"---\nname: {name}\ndescription: {description}\nversion: 0.1.0\n---\n\n"
        f"{body}\n"
    )
    text = fm.set_metadata(text, {
        "origin": "agent", "status": "provisional", "created": time.strftime("%Y-%m-%d", time.gmtime()),
        "sources": ["owner-requested"], "tainted": True,
    })
    manifest = parse_skill_md_text(text)
    if manifest:
        raise ApplyRefused(f"generated skill is invalid: {manifest}")

    change = PlannedChange(
        skill=name,
        op="create",
        files={SKILL_FILE: text},
        is_new=True,
        provenance={"sources": ["owner-requested"], "tainted": True, "source_events": []},
        diff=f"create {name}/SKILL.md ({len(text.encode('utf-8'))} bytes; sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()})",
        version_after="0.1.0",
    )
    return apply_change(
        change, skills_dir=skills_dir, review_id=f"owner-{uuid.uuid4().hex[:12]}",
        actor="owner", reason="owner-requested provisional skill",
    )


def update_requested_skill(
    *, name: str, old_text: str, new_text: str, skills_dir: Path,
) -> Applied:
    """Apply one owner-approved, exact body replacement to an existing skill.

    Frontmatter and every supporting file are out of reach. The exact old text
    must occur once, and apply_change snapshots and validates the result.
    """
    name = str(name or "").strip()
    old_text = str(old_text or "")
    new_text = str(new_text or "")
    problems = check_name(name)
    if problems:
        raise ApplyRefused("; ".join(problems))
    if not old_text.strip():
        raise ApplyRefused("old_text must be a non-empty exact excerpt from the skill body")
    if len(new_text.encode("utf-8")) > 16_000:
        raise ApplyRefused("replacement text exceeds the 16,000-byte limit")
    problems = scan_added_text(new_text)
    if problems:
        raise ApplyRefused("replacement was refused: " + "; ".join(problems))

    path = skills_dir / name / SKILL_FILE
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApplyRefused(f"could not read {name}/SKILL.md: {exc}") from exc
    frontmatter, _ = parse_frontmatter(current)
    if str(frontmatter.get("name") or "").strip() != name:
        raise ApplyRefused("skill name in SKILL.md does not match its directory; refusing to edit it")
    lines = current.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ApplyRefused("skill has no parseable frontmatter; refusing to edit it")
    body_start = len(lines[0])
    closing = False
    for index, line in enumerate(lines[1:], start=1):
        body_start += len(line)
        if line.strip() in {"---", "..."}:
            closing = True
            break
    if not closing:
        raise ApplyRefused("skill frontmatter is not closed; refusing to edit it")
    body = current[body_start:]
    hits = body.count(old_text)
    if hits != 1:
        raise ApplyRefused(f"old_text must occur exactly once in the skill body; it occurs {hits} time(s)")
    updated = current[:body_start] + body.replace(old_text, new_text, 1)
    if not updated[body_start:].strip():
        raise ApplyRefused("skill instructions must not become empty")
    updated = fm.bump_patch_version(updated, default="1.0.1")
    metadata = frontmatter.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    sources = metadata.get("sources", [])
    if not isinstance(sources, list):
        sources = [str(sources)] if sources else []
    metadata_updates: dict[str, Any] = {
        "status": "provisional", "tainted": True,
        "sources": sorted({str(source) for source in [*sources, "owner-requested"]}),
    }
    updated = fm.set_metadata(updated, metadata_updates)
    if len(updated.encode("utf-8")) > 16_000:
        raise ApplyRefused("updated SKILL.md exceeds the 16,000-byte limit")
    problem = parse_skill_md_text(updated)
    if problem:
        raise ApplyRefused(f"updated skill is invalid: {problem}")

    change = PlannedChange(
        skill=name, op="owner_patch", files={SKILL_FILE: updated}, is_new=False,
        old_files={SKILL_FILE: current},
        provenance={"sources": ["owner-requested"], "tainted": True, "source_events": []},
        diff=f"update {name}/SKILL.md ({hashlib.sha256(updated.encode('utf-8')).hexdigest()})",
        version_before=str(parse_frontmatter(current)[0].get("version") or "") or None,
        version_after=str(parse_frontmatter(updated)[0].get("version") or "") or None,
    )
    return apply_change(
        change, skills_dir=skills_dir, review_id=f"owner-{uuid.uuid4().hex[:12]}",
        actor="owner", reason="owner-approved provisional skill update",
    )


def parse_skill_md_text(text: str) -> str | None:
    """Validate frontmatter without creating a temporary file."""
    data, body = parse_frontmatter(text)
    if not data.get("name") or not data.get("description"):
        return "name and description are required"
    if not body.strip():
        return "skill instructions are empty"
    return None
