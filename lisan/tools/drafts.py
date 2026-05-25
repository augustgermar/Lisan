from __future__ import annotations

from pathlib import Path

from ..frontmatter import load_markdown, write_markdown
from .domain_fields import normalize_domain_fields, with_domain_fields
from ..tools.record_factory import new_decision, new_entity, new_episode, new_knowledge, new_open_loop, new_state
from ..utils import slugify, today_iso


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
        "compartments": fm.get("compartments", []),
        "allowed_contexts": fm.get("allowed_contexts", ["all"]),
        "blocked_contexts": fm.get("blocked_contexts", []),
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
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path


def _promote_to_state(vault: Path, fm: dict, summary: str, body: str) -> Path:
    domain_primary = str(fm.get("domain_primary", fm.get("arena_primary", "work")))
    record = new_state(
        vault,
        domain_primary,
        summary,
        arena_secondary=list(fm.get("arena_secondary", fm.get("domain_secondary", []))),
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
        review_after=str(fm.get("review_after", today_iso())),
    )
    return record.path
