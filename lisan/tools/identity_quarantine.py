"""Deterministic identity evidence gate and owner-readable candidate queue.

Weak person identities are preserved as candidate records, never minted as
entities or silently bound to an existing person. Decisions cite only source
text; the writer's summary is not treated as identity evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import write_markdown
from ..utils import slugify

IDENTITY_RESOLVER = "person_identity_gate_v1"
MIN_FULL_NAME_TOKENS = 2
MIN_CORROBORATING_SIGNALS = 1
_TITLE_WORDS = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor"}
_ROLE_WORDS = (
    "engineer", "developer", "teacher", "doctor", "nurse", "lawyer", "manager", "director",
    "administrator", "therapist", "counselor", "coach", "student", "principal",
    "founder", "employee", "coordinator", "accountant", "designer", "musician",
    "representative", "supervisor", "colleague", "coworker", "friend", "partner", "spouse",
    "daughter", "son", "mother", "father", "sister", "brother", "aunt", "uncle",
    "cousin", "wife", "husband", "girlfriend", "boyfriend", "CEO", "CTO",
)


def _name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(name.strip())}(?![\w])", re.IGNORECASE)


def exact_person_identity_paths(vault: Path, name: str) -> set[Path]:
    """Find all person records that claim this exact canonical name or alias."""
    from ..frontmatter import load_markdown
    from .entity_resolution import _entity_identity_names
    from .reference_resolution import normalize_text

    paths: set[Path] = set()
    for path in (vault / "entities").rglob("*.md"):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("type") or "") != "entity" or str(fm.get("subtype") or "") != "person":
            continue
        if any(normalize_text(value) == normalize_text(name) for value in _entity_identity_names(fm)):
            paths.add(path)
    return paths


def _excerpt(text: str, start: int, end: int, radius: int = 180) -> str:
    left = max(text.rfind(mark, 0, start) for mark in (".", "!", "?", "\n")) + 1
    stops = [pos for mark in (".", "!", "?", "\n") if (pos := text.find(mark, end)) >= 0]
    right = min(stops) + 1 if stops else len(text)
    if right - left > radius * 2:
        left, right = max(left, start - radius), min(right, end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def assess_person_identity(name: str, source_text: str) -> dict[str, Any]:
    """Require a literal full-name mention plus one source-backed signal."""
    tokens = [token for token in re.findall(r"[A-Za-z][A-Za-z'-]*", name)
              if token.casefold().rstrip(".") not in _TITLE_WORDS]
    pattern = _name_pattern(name)
    matches = list(pattern.finditer(source_text or ""))
    full_name_match = len(tokens) >= MIN_FULL_NAME_TOKENS and bool(matches)
    evidence_matches = matches
    if not evidence_matches and tokens:
        partial = re.search(rf"(?<![\w]){re.escape(tokens[0])}(?![\w])", source_text or "", re.IGNORECASE)
        if partial:
            evidence_matches = [partial]
    corroboration: list[dict[str, str]] = []
    if evidence_matches:
        for match in evidence_matches:
            snippet = _excerpt(source_text, *match.span())
            sentence_left = max(source_text.rfind(mark, 0, match.start()) for mark in (".", "!", "?", "\n")) + 1
            sentence_stops = [pos for mark in (".", "!", "?", "\n")
                              if (pos := source_text.find(mark, match.end())) >= 0]
            sentence_right = min(sentence_stops) + 1 if sentence_stops else len(source_text)
            evidence_window = source_text[max(sentence_left, match.start() - 80):
                                          min(sentence_right, match.end() + 80)]
            name_expr = re.escape(match.group(0))
            signal = None
            relation = r"(?:friend|partner|spouse|daughter|son|mother|father|sister|brother|aunt|uncle|cousin|wife|husband|girlfriend|boyfriend|colleague|coworker)"
            if re.search(rf"\b(?:my|our|his|her|their)\s+(?:\w+\s+)?{relation}\s+(?:named\s+)?{name_expr}\b", evidence_window, re.IGNORECASE) or re.search(rf"{name_expr}\b.{{0,35}}\b(?:is|was)\s+(?:my|our|his|her|their)\s+(?:\w+\s+)?{relation}\b", evidence_window, re.IGNORECASE):
                signal = "relationship_to_principal"
            elif re.search(
                rf"{name_expr}\b.{{0,35}}\b(?:and i|and we)\b.{{0,25}}\b(?:grabbed drinks|had dinner|went out|met|saw|visited|talked)\b"
                rf"|\b(?:i|we)\s+(?:met|saw|visited|texted|called|emailed|know|work with)\b.{{0,60}}{name_expr}\b",
                evidence_window, re.IGNORECASE,
            ):
                signal = "explicit_owner_statement"
            elif re.search(rf"{name_expr}\b.{{0,35}}\b(?:born|birthday|date of birth|dob)\b|\b(?:born|birthday|date of birth|dob)\b.{{0,35}}{name_expr}\b", evidence_window, re.IGNORECASE) or re.search(rf"{name_expr}\b.{{0,35}}\b(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}\b", evidence_window, re.IGNORECASE):
                signal = "date_of_birth"
            elif re.search(rf"{name_expr}\b.{{0,45}}(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{{2,}}|(?:\+?\d[\d(). -]{{7,}}\d))", evidence_window, re.IGNORECASE):
                signal = "contact_information"
            elif re.search(rf"{name_expr}\b.{{0,30}}\b(?:is|works as|serves as)\s+(?:(?:a|an|the)\s+)?(?:\w+\s+){{0,2}}(?:" + "|".join(re.escape(word) for word in _ROLE_WORDS) + r")\b", evidence_window, re.IGNORECASE):
                signal = "role_or_organization"
            elif re.search(
                rf"{name_expr}\b.{{0,40}}\b(?:at|for|with)\s+[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){{0,2}}\b",
                evidence_window,
            ):
                signal = "role_or_organization"
            elif re.search(rf"\b(?:i|we)\s+(?:know|met|work with|was introduced to|am referring to)\b.{{0,60}}{name_expr}\b|\b(?:meet|introducing)\s+{name_expr}\b|\b(?:my|her|his|their)\s+name\s+is\s+{name_expr}\b", evidence_window, re.IGNORECASE):
                signal = "explicit_owner_statement"
            if signal:
                item = {"type": signal, "excerpt": snippet}
                if item not in corroboration:
                    corroboration.append(item)
    return {
        "resolver": IDENTITY_RESOLVER,
        "candidate_name": name,
        "full_name_match": full_name_match,
        "name_evidence": [{"match": match.group(0), "match_type": "full_name" if match in matches else "partial_name_token",
                           "start": match.start(), "end": match.end(),
                           "excerpt": _excerpt(source_text, *match.span())} for match in evidence_matches],
        "corroborating_signals": corroboration,
        "qualified": full_name_match and len(corroboration) >= MIN_CORROBORATING_SIGNALS,
    }


def quarantine_identity_candidate(
    vault: Path,
    *,
    name: str,
    summary: str,
    source_ref: str,
    evidence: dict[str, Any],
    reason: str,
    matching_entities: list[str] | None = None,
) -> Path:
    """Write a deduplicated, owner-readable Markdown candidate; never mint entity."""
    root = vault / "quarantine" / "identity-candidates"
    root.mkdir(parents=True, exist_ok=True)
    source_fingerprint = hashlib.sha256(
        json.dumps({"name": name, "source_ref": source_ref, "evidence": evidence,
                    "reason": reason, "matching_entities": matching_entities or []}, sort_keys=True).encode()
    ).hexdigest()[:16]
    candidate_id = f"identity-candidate.{source_fingerprint}"
    path = root / f"{slugify(name) or 'unnamed'}-{source_fingerprint}.md"
    if not path.exists():
        fm = {
            "id": candidate_id,
            "type": "identity_candidate",
            "status": "pending_owner_review",
            "candidate_name": name,
            "proposed_summary": summary,
            "reason": reason,
            "matching_entities": matching_entities or [],
            "source_ref": source_ref,
            "evidence": evidence,
            "resolver": IDENTITY_RESOLVER,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        body = (f"# Identity candidate: {name}\n\n"
                f"Decision: **quarantined for owner review** ({reason}).\n\n"
                f"Source: `{source_ref or 'unavailable'}`\n\n"
                "The candidate has not been minted or merged. Review the cited source evidence before adjudicating.\n")
        write_markdown(path, fm, body)
    return path


def log_identity_decision(vault: Path, *, name: str, decision: str,
                          evidence: dict[str, Any], source_ref: str,
                          entity_path: str = "", reason: str = "") -> None:
    """Append one auditable person mint/quarantine decision event."""
    path = vault / "quarantine" / "identity-candidates" / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "resolver": IDENTITY_RESOLVER,
        "candidate_name": name,
        "decision": decision,
        "reason": reason,
        "source_ref": source_ref,
        "evidence": evidence,
        "entity_path": entity_path,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
