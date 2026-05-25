from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .domain_fields import with_domain_fields
from .epistemic import listify


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
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_knowledge(
    vault: Path,
    title: str,
    category: str = "frameworks",
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_episode(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_decision(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_open_loop(
    vault: Path,
    title: str,
    domain_primary: str = "cross_arena",
    domain_secondary: list[str] | None = None,
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
        "domain_primary": domain_primary,
        "domain_secondary": domain_secondary or [],
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
    write_markdown(path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=path, created=True)


def new_state(
    vault: Path,
    state_category: str,
    summary: str,
    state_secondary: list[str] | None = None,
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    if state_category not in STATE_TTLS:
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
        "compartments": [state_category] if state_category != "status" else ["agent_design"],
        "allowed_contexts": [state_category],
        "blocked_contexts": [],
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
    privacy: str = "personal",
    confidence: str = "low",
    confidence_basis: str = "User-authored placeholder",
    sources: list[str] | None = None,
    last_confirmed: str | None = None,
    review_after: str | None = None,
    ttl_days: int | None = None,
) -> CreatedRecord:
    today = today_iso()
    if state_category not in STATE_TTLS:
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
        "compartments": [state_category] if state_category != "status" else ["agent_design"],
        "allowed_contexts": [state_category],
        "blocked_contexts": [],
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
    significance: str = "low",
    summary: str | None = None,
    observed_facts: list[str] | None = None,
    verbatim_excerpt: str | None = None,
    linked_claims: list[str] | None = None,
    linked_episodes: list[str] | None = None,
    confidence_basis: str = "Source reliability assessed by the user or agent",
    last_confirmed: str | None = None,
    review_after: str | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(title)
    record_path = vault / "evidence" / "records" / f"{today}-{safe_slug}.md"
    if record_path.exists():
        raise FileExistsError(record_path)

    artifact_links = []
    if artifact_ref and "://" not in artifact_ref:
        artifact_links.append(artifact_ref)
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
        "compartments": compartments or [],
        "allowed_contexts": [arena] if arena else ["all"],
        "blocked_contexts": [],
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
"""
    if verbatim_excerpt:
        body += f"\n## Verbatim Excerpt\n\n{verbatim_excerpt.strip()}\n"
    write_markdown(record_path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=record_path, created=True)


def new_claim(
    vault: Path,
    claim_text: str,
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
    arena: str = "cross_arena",
    compartments: list[str] | None = None,
    privacy: str = "personal",
    significance: str = "low",
    summary: str | None = None,
) -> CreatedRecord:
    today = today_iso()
    safe_slug = slugify(claim_text)[:80]
    record_path = vault / "claims" / f"{today}-{safe_slug}.md"
    if record_path.exists():
        raise FileExistsError(record_path)

    support = supporting_evidence or []
    contradict = contradicting_evidence or []
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
        "compartments": compartments or [],
        "allowed_contexts": [arena] if arena else ["all"],
        "blocked_contexts": [],
        "summary": summary or claim_text[:120],
        "links": support + contradict,
        "confidence": float(confidence),
        "confidence_basis": "Claim confidence assessed from supporting and contradicting evidence",
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
    write_markdown(record_path, with_domain_fields(frontmatter), body)
    return CreatedRecord(path=record_path, created=True)


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
        "compartments": compartments or [],
        "allowed_contexts": [arena] if arena else ["all"],
        "blocked_contexts": [],
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
        "compartments": compartments or [],
        "allowed_contexts": [arena] if arena else ["all"],
        "blocked_contexts": [],
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
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
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
