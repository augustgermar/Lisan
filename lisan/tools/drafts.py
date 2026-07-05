from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from .domain_fields import normalize_domain_fields, with_domain_fields
from .record_factory import new_decision, new_entity, new_episode, new_knowledge, new_open_loop, new_state
from ..utils import slugify, today_iso

_WRITER_BLOCK_RE = re.compile(r"## Writer\n\n```json\n(.*?)\n```", re.DOTALL)

# Writer `sections` key → the episode section heading the validator requires.
_EPISODE_SECTIONS = (
    ("event_timeline", "Event Timeline"),
    ("documented_evidence", "Documented Evidence"),
    ("user_reported_context", "User-Reported Context"),
    ("interpretations", "Interpretations"),
    ("operational_consequences", "Operational Consequences"),
    ("open_questions", "Open Questions"),
)


def writer_output_from_draft(doc: Any) -> dict[str, Any] | None:
    """The Writer's structured JSON, preserved verbatim in the draft body."""
    match = _WRITER_BLOCK_RE.search(getattr(doc, "body", "") or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _iso_or_today(value: Any) -> str:
    """Writers sometimes emit prose ('after the next ingest attempt') or ISO
    durations ('P1Y') for review_after; only a real date is a date."""
    text = str(value or "").strip()[:10]
    try:
        from datetime import date

        date.fromisoformat(text)
        return text
    except ValueError:
        return today_iso()


def _render_section_items(items: Any) -> str:
    if isinstance(items, str):  # a prose section, not a list — never iterate a string
        return items.strip() or "None recorded."
    if isinstance(items, dict):
        items = [items]
    lines: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"- **{label}**: {text}" if label else f"- {text}")
        elif str(item).strip():
            lines.append(f"- {str(item).strip()}")
    return "\n".join(lines) if lines else "None recorded."


def promote_episode_from_writer(
    vault: Path,
    *,
    writer: dict[str, Any],
    draft_path: Path,
    created: str,
    source: str = "extraction",
    claim_ids: list[str] | None = None,
    entity_ids: list[str] | None = None,
) -> Path | None:
    """Build a SPEC-shaped episode record from the Writer's structured
    output — the sections it already produced, not a stub. Idempotent: an
    episode that already exists for this summary+date is left alone
    (returns None). Claims stay single-homed in their fanned-out claim
    records; the episode links to them rather than duplicating a table."""
    wfm = writer.get("frontmatter") if isinstance(writer.get("frontmatter"), dict) else {}
    summary = str(writer.get("summary") or wfm.get("summary") or "").strip()
    if not summary:
        return None
    slug = slugify(summary)[:80].rstrip("-")
    episode_path = vault / "episodes" / f"{created}-{slug}.md"
    if episode_path.exists():
        return None

    significance = str(writer.get("significance") or wfm.get("significance") or "low")
    confidence = wfm.get("confidence", "low")
    if isinstance(confidence, (int, float)):
        confidence = "high" if confidence >= 0.8 else "medium" if confidence >= 0.5 else "low"
    links = [str(l) for l in (wfm.get("links") or [])]
    try:
        links.append(str(draft_path.relative_to(vault)))
    except ValueError:
        links.append(f"drafts/{draft_path.name}")
    links.extend(claim_ids or [])

    frontmatter = {
        "id": f"episode.{created}.{slug}",
        "type": "episode",
        "created": created,
        "updated": today_iso(),
        "status": "active",
        "significance": significance,
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": summary,
        "links": links,
        "confidence": str(confidence),
        "confidence_basis": str(wfm.get("confidence_basis") or "Promoted from a skeptic-approved capture draft"),
        "last_confirmed": created,
        "review_after": _iso_or_today(wfm.get("review_after")),
        "entities": list(entity_ids or []),
        "evidence": [],
        "claims": list(claim_ids or []),
        "source": source,
        "significance_rationale": str(writer.get("significance_rationale") or ""),
    }

    sections = writer.get("sections") if isinstance(writer.get("sections"), dict) else {}
    parts = [f"# {summary}"]
    for key, heading in _EPISODE_SECTIONS:
        content = sections.get(key)
        if key == "open_questions" and not content:
            content = writer.get("questions")
        parts.append(f"## {heading}\n\n{_render_section_items(content)}")
    if claim_ids:
        claim_lines = "\n".join(f"- `{cid}`" for cid in claim_ids)
        parts.append(f"## Claims\n\nRecorded as linked claim records:\n\n{claim_lines}")
    elif significance == "high":
        parts.append("## Claims\n\nNo claims extracted for this episode.")
    body = "\n\n".join(parts) + "\n"
    write_markdown(episode_path, with_domain_fields(frontmatter), body)
    return episode_path


def promote_draft_to_episode(draft_path: Path, vault: Path) -> Path:
    if not draft_path.exists():
        raise FileNotFoundError(draft_path)
    doc = load_markdown(draft_path)
    fm = normalize_domain_fields(dict(doc.frontmatter))
    pipeline = fm.get("pipeline", {}) if isinstance(fm.get("pipeline"), dict) else {}
    task = str(pipeline.get("task") or "episode")
    summary = str(fm.get("summary", draft_path.stem))
    created = str(fm.get("created", today_iso()))

    if task == "decision":
        return _promote_to_decision(vault, fm, summary, created, doc.body)
    if task == "open_loop":
        return _promote_to_open_loop(vault, fm, summary, created, doc.body)
    if task == "state":
        return _promote_to_state(vault, fm, summary, doc.body)
    if task == "knowledge":
        return _promote_to_knowledge(vault, fm, summary, created, doc.body)
    if task == "entity":
        return _promote_to_entity(vault, fm, summary, doc.body)

    # Episode: rebuild from the Writer's structured output when the draft
    # preserves it — the real sections, not a stub.
    writer = writer_output_from_draft(doc)
    if writer:
        promoted = promote_episode_from_writer(
            vault,
            writer=writer,
            draft_path=draft_path,
            created=created,
            source=str(fm.get("source") or "extraction"),
        )
        if promoted is not None:
            return promoted

    slug = slugify(summary)
    episode_path = vault / "episodes" / f"{created}-{slug}.md"
    if episode_path.exists():
        raise FileExistsError(episode_path)

    frontmatter = {
        "id": f"episode.{created}.{slug}",
        "type": "episode",
        "created": created,
        "updated": today_iso(),
        "status": "active",
        "significance": str(fm.get("significance", "low")),
        "domain_primary": str(fm.get("domain_primary", fm.get("arena_primary", "cross_arena"))),
        "domain_secondary": fm.get("domain_secondary", fm.get("arena_secondary", [])),
        "privacy": str(fm.get("privacy", "personal")),
        "disclosure": str(fm.get("disclosure", "private")),
        "summary": summary,
        "links": fm.get("links", []),
        "confidence": str(fm.get("confidence", "low")),
        "confidence_basis": str(fm.get("confidence_basis", "Promoted from draft")),
        "last_confirmed": str(fm.get("last_confirmed", created)),
        "review_after": str(fm.get("review_after", today_iso())),
        "entities": fm.get("entities", []),
        "evidence": fm.get("evidence", []),
        "claims": fm.get("claims", []),
        "source": "manual",
        "significance_rationale": str(
            fm.get("significance_rationale")
            or ("Promoted from a high-signal capture draft." if str(fm.get("significance", "low")) == "high" else "")
        ),
    }

    raw_text = doc.body.strip()
    body = f"""# {summary}

## Event Timeline

Promoted from draft `{draft_path.name}`.

{raw_text}

## Documented Evidence

No evidence recorded yet.

## User-Reported Context

{raw_text}

## Interpretations

Draft promoted for review.

## Operational Consequences

Needs Writer and Skeptic review.

## Open Questions

What details still need confirmation?

## Claims

| ID | Claim | Type | Confidence | Source | Evidence | Status |
|----|-------|------|------------|--------|----------|--------|
"""
    write_markdown(episode_path, with_domain_fields(frontmatter), body)
    return episode_path


def _promote_to_decision(vault: Path, fm: dict, summary: str, created: str, body: str) -> Path:
    title = summary
    record = new_decision(
        vault,
        title,
        significance=str(fm.get("significance", "low")),
        summary=summary,
        links=list(fm.get("links", [])),
        confidence=str(fm.get("confidence", "low")),
        confidence_basis=str(fm.get("confidence_basis", "Promoted from draft")),
        disclosure=str(fm.get("disclosure", "private")),
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path


def _promote_to_open_loop(vault: Path, fm: dict, summary: str, created: str, body: str) -> Path:
    record = new_open_loop(
        vault,
        summary,
        significance=str(fm.get("significance", "low")),
        summary=summary,
        links=list(fm.get("links", [])),
        confidence=str(fm.get("confidence", "low")),
        confidence_basis=str(fm.get("confidence_basis", "Promoted from draft")),
        disclosure=str(fm.get("disclosure", "private")),
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path


def _promote_to_state(vault: Path, fm: dict, summary: str, body: str) -> Path:
    domain_primary = str(fm.get("domain_primary", fm.get("arena_primary", "work")))
    record = new_state(
        vault,
        domain_primary,
        summary,
        state_secondary=list(fm.get("arena_secondary", fm.get("domain_secondary", []))),
        disclosure=str(fm.get("disclosure", "private")),
        privacy=str(fm.get("privacy", "personal")),
        confidence=str(fm.get("confidence", "low")),
        confidence_basis=str(fm.get("confidence_basis", "Promoted from draft")),
        sources=list(fm.get("links", [])),
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path


def _promote_to_knowledge(vault: Path, fm: dict, summary: str, created: str, body: str) -> Path:
    record = new_knowledge(
        vault,
        summary,
        significance=str(fm.get("significance", "low")),
        summary=summary,
        links=list(fm.get("links", [])),
        confidence=str(fm.get("confidence", "low")),
        confidence_basis=str(fm.get("confidence_basis", "Promoted from draft")),
        disclosure=str(fm.get("disclosure", "private")),
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path


def _promote_to_entity(vault: Path, fm: dict, summary: str, body: str) -> Path:
    record = new_entity(
        vault,
        summary,
        subtype=str(fm.get("subtype", "person")),
        significance=str(fm.get("significance", "low")),
        summary=summary,
        confidence=str(fm.get("confidence", "low")),
        confidence_basis=str(fm.get("confidence_basis", "Promoted from draft")),
        disclosure=str(fm.get("disclosure", "private")),
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path
