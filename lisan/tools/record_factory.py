from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .domain_fields import with_domain_fields
from .epistemic import listify


ENTITY_DIRS = {
    # animate / agentive
    "person": Path("entities/people"),
    "pet": Path("entities/pets"),
    "agent": Path("entities/agents"),
    "organization": Path("entities/organizations"),
    # concrete / physical
    "place": Path("entities/places"),
    "system": Path("entities/systems"),
    "artifact": Path("entities/artifacts"),
    # abstract / conceptual
    "project": Path("entities/projects"),
    "event": Path("entities/events"),
    "topic": Path("entities/topics"),
    "account": Path("entities/accounts"),
    # fallback
    "thing": Path("entities/things"),
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


def normalize_disclosure(value: Any | None) -> str:
    key = str(value or "private").strip().lower()
    if key in {"private", "personal", "public"}:
        return key
    return "private"


_CLAIM_CLASS_ALIASES = {
    "preference": "value_statement",
    "opinion": "value_statement",
    "subjective_assessment": "interpretation",
    "subjective": "interpretation",
    "assessment": "interpretation",
    "belief": "interpretation",
    "decision": "value_statement",
    "intention": "value_statement",
    "motivation": "motive_hypothesis",
}

_CLAIM_OWNER_ALIASES = {
    "user": "user",
    "me": "user",
    "myself": "user",
    "i": "user",
    "agent": "agent",
    "assistant": "agent",
    "lisan": "agent",
    "writer": "agent",
    "skeptic": "agent",
    "interlocutor": "agent",
    "elicitor": "agent",
    "dreamer": "agent",
    "analyst": "agent",
}

_CLAIM_STATUS_ALIASES = {
    "unverified": "disputed",
    "tentative": "disputed",
    "provisional": "disputed",
    "pending": "disputed",
    "needs_revision": "disputed",
    "under_review": "disputed",
    "review_later": "disputed",
    "draft": "disputed",
    "candidate": "active",
}

_EVIDENCE_SOURCE_TYPE_ALIASES = {
    "transcript": "markdown",
    "transcript_entry": "markdown",
    "transcript-entry": "markdown",
    "transcript_excerpt": "text",
    "transcript-excerpt": "text",
    "conversation": "chat",
    "chat_transcript": "chat",
    "note": "manual_note",
}

_EVIDENCE_SENSITIVITY_ALIASES = {
    "private": "restricted",
    "personal": "restricted",
    "personal_sensitive": "restricted",
    "sensitive": "restricted",
    "confidential": "restricted",
    "secret": "sealed",
}

_STATE_CATEGORY_ALIASES = {
    "pets": "environmental",
    "pet": "environmental",
    "home": "environmental",
    "household": "environmental",
    "family_school": "status",
    "school": "status",
    "education": "status",
    "productivity": "work",
    "routine": "work",
    "habit": "work",
}


def new_entity(
    vault: Path,
    name: str,
    subtype: str = "person",
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
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
    # `kind` is an OPEN set (P3): a kind we have a directory for routes there;
    # any other (novel) kind is accepted, not rejected — it lands in
    # entities/<kind>/ (created on write) so the system never breaks on a kind
    # it hasn't seen. Only a truly empty kind degrades to the `thing` fallback.
    subtype = (subtype or "thing").strip().lower() or "thing"
    entity_dir = ENTITY_DIRS.get(subtype) or (Path("entities") / slugify(subtype))

    safe_slug = slugify(canonical_name or name)
    path = vault / entity_dir / f"{safe_slug}.md"
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary or f"{canonical_name or name} is a {subtype}.",
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "kind": subtype,
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_knowledge(
    vault: Path,
    title: str,
    category: str = "frameworks",
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary or title,
        "links": links or [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
    }
    body = f"# {title}\n\nKnowledge entry created from the CLI.\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_artifact(
    vault: Path,
    source_path: str,
    source_type: str,
    artifact_hash: str,
    file_name: str,
    file_ext: str,
    imported_at: str,
    modified_at: str,
    size_bytes: int,
    sensitivity: str = "medium",
    compartments: list[str] | None = None,
    arena: str = "cross_arena",
    source_uri: str | None = None,
    mime_type: str | None = None,
    summary: str | None = None,
    extracted_text_ref: str | None = None,
    linked_evidence: list[str] | None = None,
    linked_claims: list[str] | None = None,
    parse_errors: list[str] | None = None,
    ingestion_status: str = "discovered",
    privacy: str | None = None,
    disclosure: str | None = None,
    batch_id: str | None = None,
) -> CreatedRecord:
    today = imported_at[:10] if imported_at else today_iso()
    source_hash = hashlib.sha256(str(source_path).encode("utf-8")).hexdigest()[:8]
    artifact_hash_hex = str(artifact_hash).replace("sha256:", "").strip() or "unknown"
    safe_slug = slugify(Path(file_name).stem or Path(source_path).stem or "artifact")
    artifact_id = f"artifact.{safe_slug}.{source_hash}.{artifact_hash_hex[:8]}"
    path = vault / "evidence" / "artifacts" / f"{safe_slug}-{source_hash}-{artifact_hash_hex[:8]}.md"
    if path.exists():
        raise FileExistsError(path)

    linked_evidence = linked_evidence or []
    linked_claims = linked_claims or []
    parse_errors = parse_errors or []
    privacy = privacy or ("sealed" if sensitivity == "sealed" else "personal_sensitive" if sensitivity in {"high", "restricted"} else "personal")
    frontmatter = {
        "id": artifact_id,
        "type": "artifact",
        "created": today,
        "created_at": imported_at,
        "updated": today,
        "status": ingestion_status,
        "significance": "low",
        "domain_primary": arena,
        "domain_secondary": [],
        "arena": arena,
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary or file_name,
        "links": list(dict.fromkeys(linked_evidence + linked_claims)),
        "confidence": "low",
        "confidence_basis": "Local artifact imported from filesystem",
        "last_confirmed": today,
        "review_after": today,
        "source_type": source_type,
        "source_path": source_path,
        "source_uri": source_uri,
        "batch_id": batch_id,
        "artifact_hash": artifact_hash,
        "file_name": file_name,
        "file_ext": file_ext,
        "mime_type": mime_type,
        "size_bytes": int(size_bytes),
        "modified_at": modified_at,
        "imported_at": imported_at,
        "ingestion_status": ingestion_status,
        "sensitivity": sensitivity,
        "extracted_text_ref": extracted_text_ref,
        "linked_evidence": linked_evidence,
        "linked_claims": linked_claims,
        "parse_errors": parse_errors,
    }
    body_lines = [
        f"# Artifact: {file_name}",
        "",
        "## Source",
        "",
        f"- source_path: {source_path}",
        f"- source_uri: {source_uri or 'none'}",
        f"- source_type: {source_type}",
        f"- artifact_hash: {artifact_hash}",
        f"- file_ext: {file_ext}",
        f"- mime_type: {mime_type or 'unknown'}",
        f"- size_bytes: {int(size_bytes)}",
        f"- modified_at: {modified_at}",
        f"- imported_at: {imported_at}",
        f"- batch_id: {batch_id or 'none'}",
        "",
        "## Ingestion",
        "",
        f"- ingestion_status: {ingestion_status}",
        f"- sensitivity: {sensitivity}",
        f"- extracted_text_ref: {extracted_text_ref or 'none'}",
        f"- linked_evidence: {', '.join(linked_evidence) or 'none'}",
        f"- linked_claims: {', '.join(linked_claims) or 'none'}",
    ]
    if summary:
        body_lines.extend(["", "## Summary", "", summary.strip()])
    if parse_errors:
        body_lines.extend(["", "## Parse Errors", "", "\n".join(f"- {item}" for item in parse_errors)])
    body = "\n".join(body_lines).rstrip() + "\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_episode(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_decision(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_open_loop(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_state(
    vault: Path,
    state_category: str,
    summary: str,
    state_secondary: list[str] | None = None,
    disclosure: str | None = None,
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    state_category = normalize_state_category(state_category, summary=summary)
    if state_category is None:
        raise ValueError(f"Unsupported state category: {state_category}")
    path = vault / "state" / f"{state_category}-current.md"
    if path.exists():
        raise FileExistsError(path)

    frontmatter = {
        "id": f"state.{state_category}",
        "type": "state",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium" if state_category in {"physical", "financial", "relational", "work"} else "low",
        "domain_primary": state_category,
        "domain_secondary": state_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary,
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "ttl_days": ttl_days or STATE_TTLS[state_category],
        "sources": sources or [],
    }
    body = f"# {state_category.title()} State\n\n{summary}\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def upsert_state(
    vault: Path,
    state_category: str,
    summary: str,
    state_secondary: list[str] | None = None,
    disclosure: str | None = None,
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    state_category = normalize_state_category(state_category, summary=summary)
    if state_category is None:
        raise ValueError(f"Unsupported state category: {state_category}")
    path = vault / "state" / f"{state_category}-current.md"
    frontmatter = {
        "id": f"state.{state_category}",
        "type": "state",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium" if state_category in {"physical", "financial", "relational", "work"} else "low",
        "domain_primary": state_category,
        "domain_secondary": state_secondary or [],
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary,
        "links": [],
        "confidence": confidence,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "ttl_days": ttl_days or STATE_TTLS[state_category],
        "sources": sources or [],
    }
    body = f"# {state_category.title()} State\n\n{summary}\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_evidence(
    vault: Path,
    title: str,
    record_date: str | None = None,
    source_type: str = "manual_note",
    source_uri: str | None = None,
    artifact_ref: str | None = None,
    artifact_hash: str | None = None,
    timestamp_of_artifact: str | None = None,
    actors: list[str] | None = None,
    arena: str = "cross_arena",
    compartments: list[str] | None = None,
    sensitivity: str = "low",
    reliability: str = "medium",
    privacy: str = "personal",
    disclosure: str | None = None,
    significance: str = "low",
    summary: str | None = None,
    observed_facts: list[str] | None = None,
    verbatim_excerpt: str | None = None,
    linked_claims: list[str] | None = None,
    linked_episodes: list[str] | None = None,
    confidence_basis: str = "Source reliability assessed by the user or agent",
    last_confirmed: str | None = None,
    review_after: str | None = None,
    batch_id: str | None = None,
) -> CreatedRecord:
    today = record_date or today_iso()
    safe_slug = slugify(title)
    record_path = vault / "evidence" / "records" / f"{today}-{safe_slug}.md"
    if record_path.exists():
        raise FileExistsError(record_path)

    artifact_links = []
    if artifact_ref and "://" not in artifact_ref:
        artifact_links.append(artifact_ref)
    source_type = normalize_evidence_source_type(source_type)
    sensitivity = normalize_evidence_sensitivity(sensitivity)
    frontmatter = {
        "id": f"evidence.{safe_slug}",
        "type": "evidence",
        "created": today,
        "created_at": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "domain_primary": arena,
        "domain_secondary": compartments or [],
        "arena": arena,
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary or title,
        "links": artifact_links + listify(linked_claims) + listify(linked_episodes),
        "confidence": reliability,
        "confidence_basis": confidence_basis,
        "last_confirmed": last_confirmed or today,
        "review_after": review_after or today,
        "source_type": source_type,
        "source_uri": source_uri,
        "artifact_ref": artifact_ref,
        "artifact_hash": artifact_hash,
        "timestamp_of_artifact": timestamp_of_artifact,
        "batch_id": batch_id,
        "actors": actors or [],
        "sensitivity": sensitivity,
        "reliability": reliability,
        "observed_facts": observed_facts or [],
        "verbatim_excerpt": verbatim_excerpt or "",
        "linked_claims": linked_claims or [],
        "linked_episodes": linked_episodes or [],
    }
    body = f"""# {title}

## Summary

{summary or title}

## Observed Facts

{chr(10).join(f"- {fact}" for fact in (observed_facts or [])) or "- None recorded."}

## Source

- source_type: {source_type}
- source_uri: {source_uri or 'none'}
- artifact_ref: {artifact_ref or 'none'}
- artifact_hash: {artifact_hash or 'none'}
- timestamp_of_artifact: {timestamp_of_artifact or 'none'}
- batch_id: {batch_id or 'none'}
"""
    if verbatim_excerpt:
        body += f"\n## Verbatim Excerpt\n\n{verbatim_excerpt.strip()}\n"
    write_markdown(record_path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=record_path, created=True)


def new_claim(
    vault: Path,
    claim_text: str,
    record_date: str | None = None,
    claim_class: str = "interpretation",
    owner: str = "user",
    status: str = "active",
    confidence: float = 0.5,
    supporting_evidence: list[str] | None = None,
    contradicting_evidence: list[str] | None = None,
    linked_patterns: list[str] | None = None,
    first_seen: str | None = None,
    last_reviewed: str | None = None,
    review_notes: str = "",
    source_type: str | None = None,
    source_uri: str | None = None,
    artifact_ref: str | None = None,
    artifact_hash: str | None = None,
    timestamp_of_artifact: str | None = None,
    batch_id: str | None = None,
    arena: str = "cross_arena",
    compartments: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
    significance: str = "low",
    summary: str | None = None,
    confidence_basis: str = "Claim confidence assessed from supporting and contradicting evidence",
) -> CreatedRecord:
    today = record_date or today_iso()
    safe_slug = slugify(claim_text)[:80]
    record_path = vault / "claims" / f"{today}-{safe_slug}.md"
    if record_path.exists():
        raise FileExistsError(record_path)

    support = supporting_evidence or []
    contradict = contradicting_evidence or []
    artifact_links = []
    if artifact_ref and "://" not in artifact_ref:
        artifact_links.append(artifact_ref)
    claim_class = normalize_claim_class(claim_class)
    owner = normalize_claim_owner(owner)
    status = normalize_claim_status(status)
    privacy = normalize_claim_privacy(privacy)
    frontmatter = {
        "id": f"claim.{safe_slug}",
        "type": "claim",
        "created": today,
        "created_at": today,
        "updated": today,
        "status": status,
        "significance": significance,
        "domain_primary": arena,
        "domain_secondary": compartments or [],
        "arena": arena,
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary or claim_text[:120],
        "links": artifact_links + support + contradict,
        "confidence": float(confidence),
        "confidence_basis": confidence_basis,
        "last_confirmed": today,
        "review_after": today,
        "claim_text": claim_text,
        "claim_class": claim_class,
        "owner": owner,
        "supporting_evidence": support,
        "contradicting_evidence": contradict,
        "linked_patterns": linked_patterns or [],
        "first_seen": first_seen or today,
        "last_reviewed": last_reviewed or today,
        "review_notes": review_notes,
        "source_type": source_type,
        "source_uri": source_uri,
        "artifact_ref": artifact_ref,
        "artifact_hash": artifact_hash,
        "timestamp_of_artifact": timestamp_of_artifact,
        "batch_id": batch_id,
    }
    body = f"""# Claim

## Claim Text

{claim_text}

## Support

{chr(10).join(f"- {item}" for item in support) or "- None recorded."}

## Contradiction

{chr(10).join(f"- {item}" for item in contradict) or "- None recorded."}
"""
    if review_notes:
        body += f"\n## Review Notes\n\n{review_notes.strip()}\n"
    if artifact_ref or source_type or source_uri:
        body += "\n## Source\n\n"
        body += f"- source_type: {source_type or 'none'}\n"
        body += f"- source_uri: {source_uri or 'none'}\n"
        body += f"- artifact_ref: {artifact_ref or 'none'}\n"
        body += f"- artifact_hash: {artifact_hash or 'none'}\n"
        body += f"- timestamp_of_artifact: {timestamp_of_artifact or 'none'}\n"
        body += f"- batch_id: {batch_id or 'none'}\n"
    write_markdown(record_path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=record_path, created=True)


def normalize_claim_class(value: Any) -> str:
    key = _normalized_key(value)
    allowed = {
        "observation",
        "inference",
        "interpretation",
        "prediction",
        "motive_hypothesis",
        "value_statement",
        "identity_claim",
        "psychological_hypothesis",
    }
    return _CLAIM_CLASS_ALIASES.get(key, key if key in allowed else "interpretation")


def normalize_claim_owner(value: Any) -> str:
    from ..paths import vault_root
    from .primer_index import principal_aliases
    key = _normalized_key(value)
    # Explicit role/pronoun aliases win first: i/me/myself -> user, agent names -> agent.
    if key in _CLAIM_OWNER_ALIASES:
        return _CLAIM_OWNER_ALIASES[key]
    # Only the principal's own name tokens resolve to "user". A third party in the
    # known cast (Dana, Dana, ...) is an external_actor, not the user.
    if key in {_normalized_key(name) for name in principal_aliases(vault_root())}:
        return "user"
    return "external_actor"


def normalize_claim_status(value: Any) -> str:
    key = _normalized_key(value)
    allowed = {"active", "disputed", "confirmed", "rejected", "stale", "superseded"}
    if key in allowed:
        return key
    return _CLAIM_STATUS_ALIASES.get(key, "active")


_VALID_CLAIM_PRIVACY = {
    "personal", "personal_sensitive", "family", "legal",
    "work", "financial", "health", "children", "business", "sealed",
}
_CLAIM_PRIVACY_ALIASES: dict[str, str] = {
    "public": "personal",
    "private": "personal",
    "sensitive": "personal_sensitive",
    "restricted": "personal_sensitive",
    "confidential": "personal_sensitive",
}


def normalize_claim_privacy(value: Any) -> str:
    key = _normalized_key(value)
    if key in _VALID_CLAIM_PRIVACY:
        return key
    return _CLAIM_PRIVACY_ALIASES.get(key, "personal")


def normalize_evidence_source_type(value: Any) -> str:
    key = _normalized_key(value)
    allowed = {
        "email",
        "text",
        "calendar",
        "ticket",
        "document",
        "financial_txn",
        "chat",
        "journal",
        "browser_event",
        "git_commit",
        "file",
        "manual_note",
        "other",
        "markdown",
        "pdf",
        "image",
        "email_export",
        "sms_export",
    }
    if key in allowed:
        return key
    return _EVIDENCE_SOURCE_TYPE_ALIASES.get(key, "manual_note")


def normalize_evidence_sensitivity(value: Any) -> str:
    key = _normalized_key(value)
    allowed = {"low", "medium", "high", "restricted", "sealed"}
    if key in allowed:
        return key
    return _EVIDENCE_SENSITIVITY_ALIASES.get(key, "low")


def normalize_state_category(state_category: Any, summary: str | None = None) -> str | None:
    key = _normalized_key(state_category)
    if key in STATE_TTLS:
        return key
    if key in _STATE_CATEGORY_ALIASES:
        return _STATE_CATEGORY_ALIASES[key]
    lowered_summary = (summary or "").lower()
    if any(term in lowered_summary for term in ("cat", "dog", "pet", "animal")):
        return "environmental"
    if any(term in lowered_summary for term in ("school", "teacher", "class", "grade", "placement", "student")):
        return "status"
    if any(term in lowered_summary for term in ("morning", "routine", "workflow", "productivity", "capture")):
        return "work"
    if any(term in lowered_summary for term in ("mom", "dad", "wife", "husband", "daughter", "son", "family")):
        return "relational"
    return None


def _normalized_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split()).replace(" ", "_")


def new_skeptical_review(
    vault: Path,
    reviewed_record_id: str,
    reviewed_record_type: str,
    summary: str,
    approved: bool,
    risk: str = "medium",
    recommended_action: str = "revise",
    issues: list[dict[str, Any]] | None = None,
    priority_questions: list[str] | None = None,
    alternative_hypotheses: list[str] | None = None,
    evidence_needed: list[str] | None = None,
    claim_updates: list[dict[str, Any]] | None = None,
    confidence_adjustments: list[dict[str, Any]] | None = None,
    reasoning_errors: list[str] | None = None,
    approved_for_dreamer: bool = False,
    pattern_status: str | None = None,
    counterexample_search: dict[str, Any] | None = None,
    integration_override: dict[str, Any] | None = None,
    arena: str = "cross_arena",
    compartments: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
    significance: str = "medium",
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(f"{reviewed_record_id}-{summary}")[:80]
    record_path = vault / "reviews" / f"{today}-{safe_slug}.md"
    if record_path.exists():
        raise FileExistsError(record_path)

    issues = issues or []
    priority_questions = priority_questions or []
    alternative_hypotheses = alternative_hypotheses or []
    evidence_needed = evidence_needed or []
    claim_updates = claim_updates or []
    confidence_adjustments = confidence_adjustments or []
    reasoning_errors = reasoning_errors or []
    frontmatter = {
        "id": f"skeptical_review.{safe_slug}",
        "type": "skeptical_review",
        "created": today,
        "created_at": today,
        "updated": today,
        "status": "active",
        "significance": significance,
        "domain_primary": arena,
        "domain_secondary": compartments or [],
        "arena": arena,
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": summary,
        "links": [reviewed_record_id],
        "confidence": "low" if risk == "high" else "medium",
        "confidence_basis": "Skeptic review of draft or memory record",
        "last_confirmed": today,
        "review_after": today,
        "reviewed_record_id": reviewed_record_id,
        "reviewed_record_type": reviewed_record_type,
        "approved": approved,
        "approved_for_dreamer": approved_for_dreamer,
        "risk": risk,
        "recommended_action": recommended_action,
        "issues": issues,
        "priority_questions": priority_questions,
        "alternative_hypotheses": alternative_hypotheses,
        "evidence_needed": evidence_needed,
        "claim_updates": claim_updates,
        "confidence_adjustments": confidence_adjustments,
        "reasoning_errors": reasoning_errors,
        "pattern_status": pattern_status or "",
        "counterexample_search": counterexample_search or {
            "performed": False,
            "search_terms": [],
            "result_summary": "",
            "counterexamples": [],
        },
        "integration_override": integration_override or {
            "enabled": False,
            "reason": "",
            "approved_by": "",
        },
    }
    body = f"""# Skeptical Review

## Summary

{summary}

## Issues

{chr(10).join(f"- {issue.get('type')}: {issue.get('message')}" for issue in issues) or "- None recorded."}

## Alternative Hypotheses

{chr(10).join(f"- {item}" for item in alternative_hypotheses) or "- None recorded."}

## Evidence Needed

{chr(10).join(f"- {item}" for item in evidence_needed) or "- None recorded."}
"""
    write_markdown(record_path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=record_path, created=True)


def new_pattern(
    vault: Path,
    pattern_type: str,
    hypothesis: str,
    supporting_records: list[str] | None = None,
    counterexamples: list[str] | None = None,
    alternative_explanations: list[str] | None = None,
    confidence: float = 0.35,
    status: str = "candidate",
    first_seen: str | None = None,
    last_reviewed: str | None = None,
    predictions: list[str] | None = None,
    review_notes: str = "",
    evidence_needed: list[str] | None = None,
    counterexample_search: dict[str, Any] | None = None,
    strength_override: bool = False,
    integration_override: dict[str, Any] | None = None,
    arena: str = "cross_arena",
    compartments: list[str] | None = None,
    privacy: str = "personal",
    disclosure: str | None = None,
    significance: str = "medium",
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(f"{pattern_type}-{hypothesis}")[:80]
    path = vault / "patterns" / f"{today}-{safe_slug}.md"
    if path.exists():
        raise FileExistsError(path)

    support = supporting_records or []
    counter = counterexamples or ["No explicit counterexamples found in the scanned records."]
    alternatives = alternative_explanations or []
    preds = predictions or []
    needed = evidence_needed or []
    counterexample_search = counterexample_search or {
        "performed": True,
        "search_terms": [],
        "result_summary": "Counterexample search completed.",
        "counterexamples": counter,
    }
    integration_override = integration_override or {"enabled": False, "reason": "", "approved_by": ""}
    frontmatter = {
        "id": f"pattern.{safe_slug}",
        "type": "pattern",
        "created": today,
        "created_at": today,
        "updated": today,
        "status": status,
        "significance": significance,
        "domain_primary": arena,
        "domain_secondary": compartments or [],
        "arena": arena,
        "privacy": privacy,
        "disclosure": normalize_disclosure(disclosure),
        "summary": hypothesis[:120],
        "links": support,
        "confidence": float(confidence),
        "confidence_basis": "Analyst longitudinal pattern hypothesis",
        "last_confirmed": today,
        "review_after": today,
        "pattern_type": pattern_type,
        "hypothesis": hypothesis,
        "supporting_records": support,
        "counterexamples": counter,
        "alternative_explanations": alternatives,
        "first_seen": first_seen or today,
        "last_reviewed": last_reviewed or today,
        "predictions": preds,
        "review_notes": review_notes,
        "evidence_needed": needed,
        "counterexample_search": counterexample_search,
        "strength_override": bool(strength_override),
        "integration_override": integration_override,
    }
    body = f"""# Pattern Hypothesis

## Hypothesis

{hypothesis}

## Pattern Type

{pattern_type}

## Supporting Records

{chr(10).join(f"- {item}" for item in support) or "- None recorded."}

## Counterexample Search

- performed: {str(bool(counterexample_search.get("performed", False))).lower()}
- search_terms: {", ".join(str(term) for term in listify(counterexample_search.get("search_terms"))) or "none"}
- result_summary: {counterexample_search.get("result_summary", "Counterexample search completed.")}

## Counterexamples

{chr(10).join(f"- {item}" for item in counter) or "- None recorded."}

## Alternative Explanations

{chr(10).join(f"- {item}" for item in alternatives) or "- None recorded."}

## Predictions

{chr(10).join(f"- {item}" for item in preds) or "- None recorded."}

## Evidence Needed

{chr(10).join(f"- {item}" for item in needed) or "- None recorded."}

## Governance

- strength_override: {str(bool(strength_override)).lower()}
- integration_override.enabled: {str(bool((integration_override or {}).get("enabled", False))).lower()}
- integration_override.reason: {(integration_override or {}).get("reason", "")}
- integration_override.approved_by: {(integration_override or {}).get("approved_by", "")}
"""
    if review_notes:
        body += f"\n## Review Notes\n\n{review_notes.strip()}\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_evidence_correction(
    vault: Path,
    evidence_record_path: Path,
    field_corrected: str,
    original_value: str,
    corrected_value: str,
    basis: str,
    approved_by: str = "user",
    disclosure: str | None = None,
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
        "id": f"evidence_correction.{safe_slug}",
        "type": "evidence_correction",
        "created": today,
        "created_at": today,
        "updated": today,
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "arena": "cross_arena",
        "privacy": "personal",
        "disclosure": normalize_disclosure(disclosure),
        "summary": f"Correction for {evidence_id} field {field_corrected}",
        "links": [evidence_id],
        "confidence": "low",
        "confidence_basis": basis,
        "last_confirmed": today,
        "review_after": today,
        "corrects": evidence_id,
        "date": today,
        "field_corrected": field_corrected,
        "original_value": original_value,
        "corrected_value": corrected_value,
        "basis": basis,
        "approved_by": approved_by,
    }
    body = f"# Evidence Correction\n\nCorrection for `{evidence_record_path.stem}`.\n"
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def supersede_record(vault: Path, record_id: str, db_path: Path | None = None) -> bool:
    """Mark an existing record as superseded in-place. Returns True if updated."""
    import sqlite3 as _sqlite3
    from ..paths import sqlite_path
    _db = db_path or sqlite_path()
    if not _db.exists():
        return False
    conn = _sqlite3.connect(_db)
    try:
        row = conn.execute("SELECT path FROM files WHERE id = ?", (record_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    record_path = vault / row[0]
    if not record_path.exists():
        return False
    doc = load_markdown(record_path)
    fm = dict(doc.frontmatter)
    if str(fm.get("status", "")) == "superseded":
        return False
    fm["status"] = "superseded"
    fm["updated"] = today_iso()
    write_markdown(record_path, with_domain_fields(fm), doc.body)
    return True


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
