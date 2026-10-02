"""Layer B, part 1: deterministic first-person episodes (Phase 2 WO-4).

Prevention over filtering: self-episodes are assembled mechanically from
records that already exist — job outcomes, scheduled-task deliveries, plan
runs, ceremony artifacts, kernel drift events. No model writes them, so
the agent structurally cannot confabulate its own history; the narration
is a template over source fields, and every episode carries ``source_refs``
pointing at the records it was assembled from.

Perspective follows the substrate convention: ``{{self}}`` is the agent,
``{{principal}}`` is the owner; tokens are rendered at read time.

Idempotent by construction: every event derives a stable id, and an
episode whose file already exists is never rewritten.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..frontmatter import write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import today_iso
from .domain_fields import with_domain_fields
from .log import log_error

# Biography-grade job types: things the agent *did* on the owner's behalf,
# not background metabolism (capture.observe, index rebuilds, story rewrites).
# "task.codex" is the pre-rename spelling of task.run_codex, kept for old rows.
# Listing it alone meant executed codex tasks never reached the biography.
BIOGRAPHY_JOB_TYPES = ("task.reminder", "task.prompt", "task.run_codex", "task.codex", "plan.run", "agent.delegate")


@dataclass(slots=True)
class SelfEvent:
    event_id: str  # stable, idempotency key
    event_kind: str  # task | plan | ceremony | drift | failure | self_repair
    date: str  # YYYY-MM-DD the event happened
    title: str
    narration: str  # deterministic template output
    outcome: str  # succeeded | failed | ratified | drifted
    source_refs: list[str] = field(default_factory=list)
    significance: str = "low"
    skill: str | None = None  # event_kind "skill": which skill was used


def _safe_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:60] or "event"


# ── Event collection (deterministic queries) ─────────────────────────────────


def job_events(db_path: Path | None = None) -> list[SelfEvent]:
    db = db_path or sqlite_path()
    if not Path(db).exists():
        return []
    events: list[SelfEvent] = []
    try:
        conn = _db_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, job_type, status, payload_json, finished_at, error, attempts "
                "FROM jobs WHERE job_type IN ({q}) AND status IN ('succeeded','failed') "
                "AND finished_at IS NOT NULL".format(q=",".join("?" * len(BIOGRAPHY_JOB_TYPES))),
                BIOGRAPHY_JOB_TYPES,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log_error(vault_root(), "self_episodes.job_events query failed", exc)
        return []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        subject = str(
            payload.get("text") or payload.get("message") or payload.get("prompt")
            or payload.get("task") or payload.get("title") or payload.get("brief") or ""
        ).strip()
        date = str(row["finished_at"] or "")[:10]
        if row["job_type"] == "agent.delegate":
            # A brief is a whole prompt; the biography wants its first line.
            subject = " ".join(subject.splitlines()[:1])[:160]
        kind = "plan" if row["job_type"] == "plan.run" else "task"
        if row["status"] == "succeeded":
            if kind == "plan":
                narration = "{{self}} completed a multi-step plan for {{principal}}"
            elif row["job_type"] == "agent.delegate":
                narration = "{{self}} delegated a task to a worker for {{principal}}, and it finished"
            else:
                narration = "{{self}} carried out a scheduled task for {{principal}}"
            if subject: narration += f': "{subject}"'
            narration += "."
            outcome = "succeeded"
        else:
            what = f' "{subject}"' if subject else ""
            if row["job_type"] == "agent.delegate":
                narration = f"{{{{self}}}} delegated a task to a worker{what}, and it failed"
            else:
                narration = (
                    f"{{{{self}}}} tried to run a {row['job_type']} job{what} and failed "
                    f"after {row['attempts']} attempt(s)"
                )
            error = str(row["error"] or "").strip()
            if error:
                narration += f" — {error[:160]}"
            narration += "."
            outcome = "failed"
        title = subject[:70] if subject else f"{row['job_type']} on {date}"
        events.append(
            SelfEvent(
                event_id=f"job-{row['id']}",
                event_kind=kind,
                date=date or today_iso(),
                title=title,
                narration=narration,
                outcome=outcome,
                source_refs=[f"jobs:{row['id']}"],
                significance="medium" if outcome == "failed" else "low",
            )
        )
    return events


def ceremony_events(vault: Path) -> list[SelfEvent]:
    events: list[SelfEvent] = []
    reports = vault / "reports"
    if reports.exists():
        for path in sorted(reports.glob("voice-extraction-*.md")):
            stamp = path.stem.replace("voice-extraction-", "")
            date = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 and stamp[:8].isdigit() else today_iso()
            events.append(
                SelfEvent(
                    event_id=f"ceremony-{stamp}",
                    event_kind="ceremony",
                    date=date,
                    title="Voice extraction and ratification",
                    narration=(
                        "{{self}} distilled its own voice from its accumulated conversation "
                        "history — the extraction pass produced evidence-gated invariants, "
                        "recorded in the ratification artifact. This is how {{self}}'s "
                        "kernel voice came to be."
                    ),
                    outcome="ratified",
                    source_refs=[f"reports/{path.name}"],
                    significance="high",
                )
            )
    drift = vault / "reports" / "kernel-drift.md"
    if drift.exists():
        try:
            for line in drift.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^- (?P<ts>[0-9T:+\-\.]+) — ", line)
                if not match:
                    continue
                ts = match.group("ts")
                events.append(
                    SelfEvent(
                        event_id=f"drift-{_safe_slug(ts)}",
                        event_kind="drift",
                        date=ts[:10],
                        title="Kernel changed outside a ceremony",
                        narration=(
                            "{{self}}'s identity kernel was edited outside a ceremony — "
                            "most likely {{principal}}'s own hand. The drift was detected "
                            "by the content hash and recorded."
                        ),
                        outcome="drifted",
                        source_refs=["reports/kernel-drift.md"],
                        significance="medium",
                    )
                )
        except OSError as exc:
            log_error(vault, "self_episodes.ceremony_events drift read failed", exc)
    return events


_SKILL_OK = {"no_error_seen", "succeeded"}
_SKILL_FAILED = {"tool_error", "failed"}


def _skill_event(row: Any) -> SelfEvent | None:
    """One skill use as a first-person episode. An outcome that is neither clearly
    fine nor clearly an error is not an episode: the belief gate must not be fed
    guesses."""
    import hashlib

    outcome = str(row["outcome"] or "")
    if outcome in _SKILL_OK:
        result, wording = "succeeded", "it ran without an error"
    elif outcome in _SKILL_FAILED:
        result, wording = "failed", "it hit an error"
    else:
        return None
    skill, event_id = str(row["skill"]), str(row["event_id"])
    # A use inside a plan has no learning event of its own; its job is the source.
    ref = f"jobs:{event_id.split(':', 1)[1]}" if event_id.startswith("plan-turn:") else f"learning:{event_id}"
    return SelfEvent(
        event_id=f"skill-{skill}-{hashlib.sha1(event_id.encode()).hexdigest()[:10]}",
        event_kind="skill",
        date=str(row["used_at"] or "")[:10] or today_iso(),
        title=f"Used the {skill} skill",
        # Honest about what is known: "without an error" is not "it worked".
        narration=f"{{{{self}}}} used the {skill} skill for {{{{principal}}}}, and {wording}.",
        outcome=result,
        source_refs=[ref],
        significance="medium" if result == "failed" else "low",
        skill=skill,
    )


def skill_events(db_path: Path | None = None) -> list[SelfEvent]:
    """Skill use, from the learning loop's usage ledger: how the agent's own
    procedures have fared is part of its autobiography, and the belief extractor
    forms "I can rely on X" / "X fails often" from exactly these."""
    db = db_path or sqlite_path()
    if not Path(db).exists():
        return []
    try:
        conn = _db_connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT skill, event_id, used_at, outcome FROM skill_usage ORDER BY used_at").fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []  # no ledger yet: no learning has been recorded on this install
    return [e for e in (_skill_event(r) for r in rows) if e is not None]


def collect_events(vault: Path, db_path: Path | None = None) -> list[SelfEvent]:
    return sorted(job_events(db_path) + ceremony_events(vault) + skill_events(db_path), key=lambda e: (e.date, e.event_id))


def record_skill_episodes(vault: Path, db_path: Path | None, event_ids: list[str]) -> int:
    """Post-recording hook: the skill uses of these learning events become episodes
    now, not at the next catch-up pass. Never raises into the work it observes."""
    written = 0
    try:
        wanted = set(event_ids)
        for event in skill_events(db_path):
            ref = event.source_refs[0]
            source = ref.split(":", 1)[1] if ref.startswith("learning:") else ("plan-turn:" + ref.split(":", 1)[1])
            if source in wanted and write_self_episode(vault, event, db_path) is not None:
                written += 1
    except Exception as exc:
        try:
            log_error(vault, "self_episodes.record_skill_episodes failed", exc)
        except Exception:
            pass
    return written


# ── Assembly (idempotent writes) ─────────────────────────────────────────────


def episode_path(vault: Path, event: SelfEvent) -> Path:
    return vault / "self" / "episodes" / f"{event.date}-{_safe_slug(event.event_id)}.md"


def _quarantined(vault: Path, filename: str) -> bool:
    """True if the owner has set this episode aside. A quarantine is a sibling
    `quarantine*/self-episodes/` folder beside the vault (moved, not deleted, so it
    can be restored). The catch-up pass rebuilds every episode it can derive from
    the job table, so without this check it would silently undo a quarantine: it
    regenerated 149 episodes from the 2026-07-27 plan-recursion incident, which the
    owner had deliberately removed, the first time it was run after the quarantine."""
    try:
        return any((d / "self-episodes" / filename).exists() for d in vault.parent.glob("quarantine*") if d.is_dir())
    except OSError:
        return False


def write_self_episode(vault: Path, event: SelfEvent, db_path: Path | None = None) -> Path | None:
    """Write one first-person episode; returns None if it already exists or the
    owner has quarantined it."""
    path = episode_path(vault, event)
    if path.exists() or _quarantined(vault, path.name):
        return None
    today = today_iso()
    frontmatter = {
        "id": f"self_episode.{_safe_slug(event.event_id)}",
        "type": "self_episode",
        "created": event.date,
        "updated": today,
        "status": "active",
        "significance": event.significance,
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        # The summary is MY act, not the owner's content — a reminder about
        # the owner's oven is not an event in my life; delivering it is.
        # (Found live: the first post-wipe self-episode's summary was the
        # bare task text "Turn the oven on for the pork loin.")
        "summary": event.narration,
        "title": event.title,
        "links": [],
        "confidence": "high",
        "confidence_basis": "Assembled deterministically from system records",
        "last_confirmed": today,
        "review_after": today,
        "event_kind": event.event_kind,
        "source_refs": event.source_refs,
        "outcome": event.outcome,
    }
    if event.skill:
        frontmatter["skill"] = event.skill
    body = (
        f"# {event.title}\n\n## What happened\n\n{event.narration}\n\n"
        "## Sources\n\n" + "\n".join(f"- `{ref}`" for ref in event.source_refs) + "\n"
    )
    write_markdown(path, with_domain_fields(frontmatter), body)
    # An unindexed autobiography is invisible to retrieval — index now.
    from .rebuild_index import index_record_best_effort

    index_record_best_effort(vault, path, db_path)
    return path


def assemble_self_episodes(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """The backfill / catch-up pass: idempotent over all known events."""
    vault = vault or vault_root()
    written: list[str] = []
    for event in collect_events(vault, db_path):
        try:
            path = write_self_episode(vault, event, db_path)
        except Exception as exc:
            log_error(vault, f"self_episodes.write failed for {event.event_id}", exc)
            continue
        if path is not None:
            written.append(str(path))
    return {"written": len(written), "paths": written}


def record_job_episode(vault: Path, job: dict[str, Any], db_path: Path | None = None) -> None:
    """Post-completion hook: one finished biography-grade job → one episode,
    immediately. Failures here must never break job processing."""
    try:
        if str(job.get("job_type") or "") not in BIOGRAPHY_JOB_TYPES:
            return
        for event in job_events(db_path):
            if event.event_id == f"job-{job.get('id')}":
                write_self_episode(vault, event, db_path)
                return
    except Exception as exc:
        try:
            log_error(vault, "self_episodes.record_job_episode failed", exc)
        except Exception:
            pass
