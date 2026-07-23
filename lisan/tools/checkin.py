"""Ship 1 of WO-PSYCHE: the observation and support layer.

Two owner-initiated capture rituals, both deliberately cheap (target:
under thirty seconds over chat) and both strictly OBSERVATIONAL — they
record what happened, never what it means. Interpretation belongs to
pattern records with their own lifecycle (see docs/psyche_workorder.md,
§1 rule 1: a retired interpretation must leave no residue in the
observational record — which is only possible if the record never
contained interpretation to begin with).

- ``record_checkin``: a dated, neutral observation about a person —
  state, optional context tags (whose day it was, school or not),
  optional direct quote. Stored as an evidence record linked to the
  person's entity; context tags are encoded as ``context: <tag>``
  observed-facts entries so the future analyst (Ship 3) can correlate
  states against contexts deterministically.
- ``support_note``: a dated outcome for a support strategy ("the
  feelings-dichotomy questions", "the personal-space game") — worked,
  didn't, or mixed. Stored as a ``support_strategy`` pattern per
  (person, strategy): a hypothesis that something HELPS, accumulating
  its evidence and counterexamples like any other hypothesis. "What
  works for Maya during transitions?" answers from record, not vibes.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .entity_merge import _find_entity
from .rebuild_index import reindex_record

_OUTCOMES = {"worked", "didnt_work", "mixed"}


def _refused(vault: Path, subject: str, error: str, *, candidates: list[str] | None = None) -> dict[str, Any]:
    """A check-in that fails to record is never silent (failure policy):
    it lands in the error log AND the tool reply instructs the model to
    tell the user, so a dropped observation is a visible event, not a
    quiet nothing (the 2026-07-23 nap check-in vanished without a trace)."""
    from .log import log_error

    try:
        log_error(vault, "checkin.refused", ValueError(f"subject={subject!r}: {error}"))
    except Exception:
        pass
    out: dict[str, Any] = {"ok": False, "recorded": False, "error": error, "known_people": _nearby_people(vault)}
    if candidates:
        out["did_you_mean"] = candidates
    return out


def _slug_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def _entity_identity(path: Path) -> tuple[str, str]:
    fm = load_markdown(path).frontmatter
    return str(fm.get("id") or path.stem), str(fm.get("canonical_name") or path.stem)


def _nearby_people(vault: Path, limit: int = 8) -> list[str]:
    people = vault / "entities" / "people"
    if not people.exists():
        return []
    names = []
    for p in sorted(people.glob("*.md"))[:50]:
        try:
            names.append(str(load_markdown(p).frontmatter.get("canonical_name") or p.stem))
        except Exception:
            continue
    return names[:limit]


_SELF_WORDS = {"me", "myself", "i", "self"}


def resolve_checkin_subject(
    vault: Path, subject: str, db_path: Path | None = None
) -> tuple[Path | None, list[str]]:
    """Resolve a check-in subject the way the owner actually talks.

    The exact matcher borrowed from entity_merge demanded merge-grade
    precision from a thirty-second capture path — "August" and "me"
    resolved to nothing while august-germar.md sat right there (the
    2026-07-23 nap check-in was dropped exactly here). Order:

    1. exact (canonical name / id / file stem / frontmatter aliases) —
       unchanged behavior;
    2. the entity_aliases index the rest of the system already maintains;
    3. self-references (me/myself/I plus the primer's principal aliases)
       resolve to the principal's person entity;
    4. unique first-name match across person entities — and on a tie we
       REFUSE with the candidates, never guess. A wrong subject on a
       child's record is worse than a re-ask.

    Returns (path, candidates): path on success; on failure, candidates
    holds did-you-mean names when the miss was an ambiguity."""
    subject = str(subject or "").strip()
    if not subject:
        return None, []
    hit = _find_entity(vault, subject)
    if hit is not None:
        return hit, []

    lowered = subject.lower()

    # Alias index (case-insensitive). Distinct targets = ambiguity.
    if db_path is None:
        from ..paths import sqlite_path

        db_path = sqlite_path()
    if db_path and Path(db_path).exists():
        try:
            from .db import connect as _db_connect

            conn = _db_connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT f.path, f.id FROM entity_aliases a "
                    "JOIN files f ON f.id = a.entity_id "
                    "WHERE a.alias = ? COLLATE NOCASE LIMIT 3",
                    (subject,),
                ).fetchall()
            finally:
                conn.close()
            if len(rows) == 1:
                candidate = vault / str(rows[0][0])
                if candidate.exists():
                    return candidate, []
            elif len(rows) > 1:
                return None, [str(r[1]) for r in rows]
        except Exception:
            pass  # a broken index degrades to the scans below, never blocks

    # Self-references resolve to the principal's own person entity.
    try:
        from .primer_index import principal_aliases

        principal = {a.lower() for a in principal_aliases(vault)}
    except Exception:
        principal = set()
    if lowered in _SELF_WORDS or lowered in principal:
        for name in sorted(principal) or [lowered]:
            match, ambiguous = _first_name_match(vault, name)
            if match is not None:
                return match, []
        return None, []

    return _first_name_match(vault, lowered)


def _first_name_match(vault: Path, lowered: str) -> tuple[Path | None, list[str]]:
    """Unique first-token match over person entities; ties refuse loudly."""
    matches: list[tuple[Path, str]] = []
    people_root = vault / "entities"
    if not people_root.exists():
        return None, []
    for p in sorted(people_root.rglob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        if str(fm.get("subtype") or fm.get("kind") or "") != "person":
            continue
        canonical = str(fm.get("canonical_name") or p.stem)
        first = canonical.strip().split()[0].lower() if canonical.strip() else ""
        if first == lowered:
            matches.append((p, canonical))
    if len(matches) == 1:
        return matches[0][0], []
    if len(matches) > 1:
        return None, [canonical for _, canonical in matches]
    return None, []


def _append_link(record_path: Path, entity_id: str) -> None:
    doc = load_markdown(record_path)
    fm = dict(doc.frontmatter)
    links = list(fm.get("links") or [])
    if entity_id not in links:
        links.append(entity_id)
    fm["links"] = links
    write_markdown(record_path, fm, doc.body)


def record_checkin(
    vault: Path,
    subject: str,
    note: str,
    *,
    tags: list[str] | None = None,
    quote: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """One neutral observation about a person, timestamped and linked.
    Refuses unknown subjects rather than minting people from typos."""
    from .record_factory import new_evidence

    note = str(note or "").strip()
    if not note:
        return _refused(vault, subject, "empty note — a check-in records something observed")
    entity_path, candidates = resolve_checkin_subject(vault, subject, db_path)
    if entity_path is None:
        if candidates:
            return _refused(
                vault,
                subject,
                f"{subject!r} is ambiguous — did you mean: {', '.join(candidates)}? "
                "Re-ask the user; never guess the subject of a check-in.",
                candidates=candidates,
            )
        return _refused(
            vault,
            subject,
            f"no entity found for {subject!r} — tell the user the check-in was NOT recorded "
            "and ask which person they meant",
        )
    entity_id, canonical = _entity_identity(entity_path)

    observed = [note]
    for tag in tags or []:
        tag = str(tag).strip()
        if tag:
            observed.append(f"context: {tag}")

    now = datetime.now().astimezone()
    created = new_evidence(
        vault,
        title=f"Check-in — {canonical} — {now.strftime('%Y-%m-%d %H:%M')}",
        source_type="checkin",
        actors=[canonical],
        observed_facts=observed,
        verbatim_excerpt=(str(quote).strip() or None) if quote else None,
        reliability="high",
        sensitivity="high",
        privacy="personal",
        disclosure="private",
        significance="low",
        summary=f"Check-in on {canonical}: {note[:120]}",
        timestamp_of_artifact=now.isoformat(timespec="seconds"),
        confidence_basis="Direct owner observation at capture time",
    )
    _append_link(created.path, entity_id)
    reindex_record(created.path, vault, db_path, quiet=True)
    return {
        "ok": True,
        "path": str(created.path),
        "subject": canonical,
        "tags": [t for t in (tags or []) if str(t).strip()],
    }


def _find_support_pattern(vault: Path, entity_id: str, strategy: str) -> Path | None:
    patterns = vault / "patterns"
    if not patterns.exists():
        return None
    key = _slug_key(strategy)
    for p in sorted(patterns.glob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        if str(fm.get("pattern_type")) != "support_strategy":
            continue
        if entity_id not in (fm.get("links") or []):
            continue
        if key and key in _slug_key(str(fm.get("hypothesis") or "")):
            return p
    return None


_NO_COUNTEREXAMPLE_PLACEHOLDER = "No explicit counterexamples found"


def support_note(
    vault: Path,
    subject: str,
    strategy: str,
    outcome: str,
    *,
    note: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """A dated outcome for a support strategy. First outcome creates the
    strategy's pattern record; later outcomes accumulate on it — worked
    and mixed as supporting evidence, didn't-work as counterexamples."""
    outcome = str(outcome or "").strip().lower().replace("'", "").replace("-", "_")
    if outcome == "didnt":
        outcome = "didnt_work"
    if outcome not in _OUTCOMES:
        return {"ok": False, "error": f"outcome must be one of {sorted(_OUTCOMES)}"}
    strategy = str(strategy or "").strip()
    if not strategy:
        return {"ok": False, "error": "empty strategy"}
    entity_path, candidates = resolve_checkin_subject(vault, subject)
    if entity_path is None:
        error = (
            f"{subject!r} is ambiguous — did you mean: {', '.join(candidates)}?"
            if candidates
            else f"no entity found for {subject!r}"
        )
        return _refused(vault, subject, error, candidates=candidates or None)
    entity_id, canonical = _entity_identity(entity_path)

    pattern_path = _find_support_pattern(vault, entity_id, strategy)
    if pattern_path is None:
        from .record_factory import new_pattern

        created = new_pattern(
            vault,
            pattern_type="support_strategy",
            hypothesis=f"{strategy} helps {canonical}",
            status="active_hypothesis",
            significance="medium",
            privacy="personal",
            disclosure="private",
            evidence_needed=["More dated outcomes across different days and moods"],
        )
        pattern_path = created.path
        _append_link(pattern_path, entity_id)

    entry = f"{today_iso()}: {outcome}" + (f" — {str(note).strip()}" if note and str(note).strip() else "")
    doc = load_markdown(pattern_path)
    fm = dict(doc.frontmatter)
    if outcome == "didnt_work":
        counters = [c for c in (fm.get("counterexamples") or []) if _NO_COUNTEREXAMPLE_PLACEHOLDER not in str(c)]
        counters.append(entry)
        fm["counterexamples"] = counters
    else:
        supporting = list(fm.get("supporting_records") or [])
        supporting.append(entry)
        fm["supporting_records"] = supporting
    fm["last_reviewed"] = today_iso()
    fm["updated"] = today_iso()
    write_markdown(pattern_path, fm, doc.body)
    reindex_record(pattern_path, vault, db_path, quiet=True)

    worked = len(fm.get("supporting_records") or [])
    didnt = len([c for c in (fm.get("counterexamples") or []) if _NO_COUNTEREXAMPLE_PLACEHOLDER not in str(c)])
    return {
        "ok": True,
        "path": str(pattern_path),
        "subject": canonical,
        "strategy": strategy,
        "tally": {"worked_or_mixed": worked, "didnt_work": didnt},
    }


def support_summary(vault: Path, subject: str) -> dict[str, Any]:
    """Every support strategy on record for a person, with tallies and the
    most recent outcomes — the 'what helps' view."""
    entity_path, candidates = resolve_checkin_subject(vault, subject)
    if entity_path is None:
        error = (
            f"{subject!r} is ambiguous — did you mean: {', '.join(candidates)}?"
            if candidates
            else f"no entity found for {subject!r}"
        )
        return _refused(vault, subject, error, candidates=candidates or None)
    entity_id, canonical = _entity_identity(entity_path)

    strategies = []
    patterns = vault / "patterns"
    if patterns.exists():
        for p in sorted(patterns.glob("*.md")):
            try:
                fm = load_markdown(p).frontmatter
            except Exception:
                continue
            if str(fm.get("pattern_type")) != "support_strategy" or entity_id not in (fm.get("links") or []):
                continue
            supporting = list(fm.get("supporting_records") or [])
            counters = [c for c in (fm.get("counterexamples") or []) if _NO_COUNTEREXAMPLE_PLACEHOLDER not in str(c)]
            strategies.append({
                "strategy": str(fm.get("hypothesis") or p.stem),
                "worked_or_mixed": len(supporting),
                "didnt_work": len(counters),
                "recent": (supporting + counters)[-3:],
            })
    return {"ok": True, "subject": canonical, "strategies": strategies}
