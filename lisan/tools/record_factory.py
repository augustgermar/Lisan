from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso


ENTITY_DIRS = {
    "person": Path("entities/people"),
    "place": Path("entities/places"),
    "thing": Path("entities/things"),
    "project": Path("entities/projects"),
    "organization": Path("entities/organizations"),
}

KNOWLEDGE_DIRS = {
    "frameworks": Path("knowledge/frameworks"),
    "legal": Path("knowledge/legal"),
    "financial": Path("knowledge/financial"),
    "technical": Path("knowledge/technical"),
}

STATE_TTLS = {
    "physical": 14,
    "environmental": 30,
    "financial": 30,
    "relational": 14,
    "work": 14,
    "status": 60,
    "appearance": 30,
    "competence": 60,
    "social_presence": 30,
    "desirability": 30,
}


@dataclass(slots=True)
class CreatedRecord:
    path: Path
    created: bool


def new_entity(
    vault: Path,
    name: str,
    subtype: str = "person",
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
    canonical_name: str | None = None,
    aliases: list[str] | None = None,
    disambiguation: str | None = None,
    compartments: list[str] | None = None,
    allowed_contexts: list[str] | None = None,
    blocked_contexts: list[str] | None = None,
    confidence: str = "low",
    confidence_basis: str = "User-provided placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    epoch: int = 1,
    epoch_started: str | None = None,
    previous_epochs: list[dict[str, Any]] | None = None,
) -> CreatedRecord:
    subtype = subtype.lower()
    if subtype not in ENTITY_DIRS:
        raise ValueError(f"Unsupported entity subtype: {subtype}")

    safe_slug = slugify(canonical_name or name)
    path = vault / ENTITY_DIRS[subtype] / f"{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    today = today_iso()
    entity_id = f"entity.{safe_slug}" if subtype == "person" else f"entity.{subtype}.{safe_slug}"
    frontmatter = {
        "id": entity_id,
        "type": "entity",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": compartments or [],
        "allowed_contexts": allowed_contexts or ["all"],
        "blocked_contexts": blocked_contexts or [],
        "summary": summary or f"{canonical_name or name} is a {subtype}.",
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "subtype": subtype,
        "canonical_name": canonical_name or name,
        "aliases": aliases or [],
        "disambiguation": disambiguation or f"Auto-generated {subtype} placeholder.",
        "epoch": epoch,
        "epoch_started": epoch_started or today,
        "previous_epochs": previous_epochs or [],
    }
    body_summary = summary or f"{canonical_name or name} is a {subtype}."
    body = f"# {canonical_name or name}\n\n{body_summary}\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_knowledge(
    vault: Path,
    title: str,
    category: str = "frameworks",
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
    links: list[str] | None = None,
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
) -> CreatedRecord:
    if category not in KNOWLEDGE_DIRS:
        raise ValueError(f"Unsupported knowledge category: {category}")
    today = today_iso()
    safe_slug = slugify(title)
    path = vault / KNOWLEDGE_DIRS[category] / f"{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"knowledge.{safe_slug}",
        "type": "knowledge",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary or title,
        "links": links or [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
    }
    body = f"# {title}\n\nKnowledge entry created from the CLI.\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_episode(
    vault: Path,
    title: str,
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    source: str = "manual",
    summary: str | None = None,
    entities: list[str] | None = None,
    evidence: list[str] | None = None,
    claims: list[str] | None = None,
    links: list[str] | None = None,
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    significance_rationale: str | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(title)
    path = vault / "episodes" / f"{today}-{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"episode.{today}.{safe_slug}",
        "type": "episode",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary or title,
        "links": links or [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "significance_rationale": significance_rationale or "",
        "entities": entities or [],
        "evidence": evidence or [],
        "claims": claims or [],
        "source": source,
    }
    body = _episode_body(title, claims_required=significance == "high")
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_decision(
    vault: Path,
    title: str,
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
    links: list[str] | None = None,
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    revisit_after: str | None = None,
    revisit_conditions: list[str] | None = None,
    alternatives_considered: list[str] | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(title)
    path = vault / "decisions" / f"{today}-{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"decision.{safe_slug}",
        "type": "decision",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary or title,
        "links": links or [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "revisit_after": revisit_after or today,
        "revisit_conditions": revisit_conditions or [],
        "alternatives_considered": alternatives_considered or [],
    }
    decision_text = summary or title
    alts = "\n".join(f"- {a}" for a in (alternatives_considered or [])) or "None recorded."
    revisit = "\n".join(f"- {r}" for r in (revisit_conditions or [])) or "None recorded."
    body = f"""# {title}

## Decision

{decision_text}

## Alternatives Considered

{alts}

## Revisit Conditions

{revisit}
"""
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_open_loop(
    vault: Path,
    title: str,
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
    links: list[str] | None = None,
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    priority: str = "medium",
    owner: str = "user",
    next_action: str = "Describe the next action.",
    blocked_by: str | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(title)
    path = vault / "open_loops" / f"{today}-{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"open_loop.{safe_slug}",
        "type": "open_loop",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary or title,
        "links": links or [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "priority": priority,
        "owner": owner,
        "next_action": next_action,
        "blocked_by": blocked_by,
    }
    body = f"# {title}\n\n## Next Action\n\n{next_action}\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_state(
    vault: Path,
    arena_primary: str,
    summary: str,
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    if arena_primary not in STATE_TTLS:
        raise ValueError(f"Unsupported state arena: {arena_primary}")
    path = vault / "state" / f"{arena_primary}-current.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"state.{arena_primary}",
        "type": "state",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium" if arena_primary in {"physical", "financial", "relational", "work"} else "low",
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [arena_primary] if arena_primary != "status" else ["agent_design"],
        "allowed_contexts": [arena_primary],
        "blocked_contexts": [],
        "summary": summary,
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "ttl_days": ttl_days or STATE_TTLS[arena_primary],
        "sources": sources or [],
    }
    body = f"# {arena_primary.title()} State\n\n{summary}\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def upsert_state(
    vault: Path,
    arena_primary: str,
    summary: str,
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    if arena_primary not in STATE_TTLS:
        raise ValueError(f"Unsupported state arena: {arena_primary}")
    path = vault / "state" / f"{arena_primary}-current.md"
    frontmatter = {
        "id": f"state.{arena_primary}",
        "type": "state",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium" if arena_primary in {"physical", "financial", "relational", "work"} else "low",
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [arena_primary] if arena_primary != "status" else ["agent_design"],
        "allowed_contexts": [arena_primary],
        "blocked_contexts": [],
        "summary": summary,
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "ttl_days": ttl_days or STATE_TTLS[arena_primary],
        "sources": sources or [],
    }
    body = f"# {arena_primary.title()} State\n\n{summary}\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def new_evidence(
    vault: Path,
    title: str,
    subtype: str = "document",
    arena_primary: str = "cross_arena",
    arena_secondary: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
    supports: list[str] | None = None,
    corrections: list[str] | None = None,
    links: list[str] | None = None,
    confidence: str = "high",
    confidence_basis: str = "User-authored placeholder",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    artifact_text: str | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(title)
    artifact_path = vault / "evidence" / "artifacts" / f"{today}-{safe_slug}.txt"
    record_path = vault / "evidence" / "records" / f"{today}-{safe_slug}.md"
    if record_path.exists() or artifact_path.exists():
        raise FileExistsError(record_path)

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(artifact_text or f"Evidence artifact placeholder for {title}.\n", encoding="utf-8")
    frontmatter = {
        "id": f"evidence.{safe_slug}",
        "type": "evidence",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "arena_primary": arena_primary,
        "arena_secondary": arena_secondary or [],
        "privacy": privacy,
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary or title,
        "links": links or [f"evidence/artifacts/{artifact_path.name}"],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "subtype": subtype,
        "date_of_artifact": today,
        "supports": supports or [],
        "corrections": corrections or [],
    }
    body = f"# {title}\n\nEvidence record for {title}.\n"
    write_markdown(record_path, frontmatter, body)
    return CreatedRecord(path=record_path, created=True)


def new_evidence_correction(
    vault: Path,
    evidence_record_path: Path,
    field_corrected: str,
    original_value: str,
    corrected_value: str,
    basis: str,
    approved_by: str = "user",
) -> CreatedRecord:
    today = today_iso()
    if not evidence_record_path.exists():
        raise FileNotFoundError(evidence_record_path)
    evidence_id = str(load_markdown(evidence_record_path).frontmatter.get("id", evidence_record_path.stem))
    safe_slug = slugify(f"{evidence_record_path.stem}-{field_corrected}")
    path = vault / "evidence" / "corrections" / f"{today}-{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "type": "evidence_correction",
        "corrects": evidence_id,
        "date": today,
        "field_corrected": field_corrected,
        "original_value": original_value,
        "corrected_value": corrected_value,
        "basis": basis,
        "approved_by": approved_by,
    }
    body = f"# Evidence Correction\n\nCorrection for `{evidence_record_path.stem}`.\n"
    write_markdown(path, frontmatter, body)
    return CreatedRecord(path=path, created=True)


def _episode_body(title: str, claims_required: bool) -> str:
    claims_block = """
## Claims

| ID | Claim | Type | Confidence | Source | Evidence | Status |
|----|-------|------|------------|--------|----------|--------|
| claim.placeholder.001 | Placeholder claim | reported | low | User-authored placeholder | null | unresolved |
""".strip()
    if not claims_required:
        claims_block = """
## Claims

| ID | Claim | Type | Confidence | Source | Evidence | Status |
|----|-------|------|------------|--------|----------|--------|
""".strip()
    return f"""# {title}

## Event Timeline

No details recorded yet.

## Documented Evidence

No evidence recorded yet.

## User-Reported Context

No additional context recorded yet.

## Interpretations

No interpretations recorded yet.

## Operational Consequences

No consequences recorded yet.

## Open Questions

No open questions recorded yet.

{claims_block}
"""
