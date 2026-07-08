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
        return {"ok": False, "error": "empty note — a check-in records something observed"}
    entity_path = _find_entity(vault, subject)
    if entity_path is None:
        return {
            "ok": False,
            "error": f"no entity found for {subject!r}",
            "known_people": _nearby_people(vault),
        }
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
    entity_path = _find_entity(vault, subject)
    if entity_path is None:
        return {"ok": False, "error": f"no entity found for {subject!r}", "known_people": _nearby_people(vault)}
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
    entity_path = _find_entity(vault, subject)
    if entity_path is None:
        return {"ok": False, "error": f"no entity found for {subject!r}", "known_people": _nearby_people(vault)}
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
