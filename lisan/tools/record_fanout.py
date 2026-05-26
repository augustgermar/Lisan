from __future__ import annotations

from typing import Any

from ..utils import slugify


def normalize_reference(value: Any) -> str:
    return " ".join(str(value).strip().lower().split())


def claim_reference_keys(entry: dict[str, Any]) -> list[str]:
    keys: set[str] = set()
    for field in ("claim_text", "summary", "title"):
        raw = str(entry.get(field) or "").strip()
        if not raw:
            continue
        keys.add(raw)
        keys.add(normalize_reference(raw))
        keys.add(slugify(raw))
    return [key for key in keys if key]


def register_claim_reference(reference_map: dict[str, str], entry: dict[str, Any], claim_id: str) -> None:
    for key in claim_reference_keys(entry):
        reference_map.setdefault(key, claim_id)
    reference_map.setdefault(normalize_reference(claim_id), claim_id)
    reference_map.setdefault(slugify(claim_id), claim_id)


def resolve_claim_links(raw_links: list[Any] | None, reference_map: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in raw_links or []:
        text = str(raw).strip()
        if not text:
            continue
        candidates = [text, normalize_reference(text), slugify(text)]
        if text.startswith("claim."):
            candidates.insert(0, text)
        match = None
        for candidate in candidates:
            if candidate in reference_map:
                match = reference_map[candidate]
                break
        if match is None and text.startswith("claim."):
            match = text
        if match and match not in seen:
            seen.add(match)
            resolved.append(match)
    return resolved


# ── Evidence references (Finding 4) ──────────────────────────────────────────
#
# The writer often produces claim/evidence link strings that are natural-language
# titles ("Transcript note: Devon staffing reflection") rather than resolvable
# IDs. We mirror the claim-id resolution pattern: build a map from every
# stringified form of an evidence entry's title to the generated evidence ID,
# then rewrite incoming link arrays through that map. Unresolvable strings are
# dropped silently so the vault validator stays clean.


def evidence_reference_keys(entry: dict[str, Any]) -> list[str]:
    keys: set[str] = set()
    for field in ("title", "summary", "verbatim_excerpt"):
        raw = str(entry.get(field) or "").strip()
        if not raw:
            continue
        keys.add(raw)
        keys.add(normalize_reference(raw))
        keys.add(slugify(raw))
    return [key for key in keys if key]


def register_evidence_reference(reference_map: dict[str, str], entry: dict[str, Any], evidence_id: str) -> None:
    for key in evidence_reference_keys(entry):
        reference_map.setdefault(key, evidence_id)
    reference_map.setdefault(normalize_reference(evidence_id), evidence_id)
    reference_map.setdefault(slugify(evidence_id), evidence_id)


def resolve_evidence_links(raw_links: list[Any] | None, reference_map: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in raw_links or []:
        text = str(raw).strip()
        if not text:
            continue
        candidates = [text, normalize_reference(text), slugify(text)]
        if text.startswith("evidence."):
            candidates.insert(0, text)
        match = None
        for candidate in candidates:
            if candidate in reference_map:
                match = reference_map[candidate]
                break
        if match is None and text.startswith("evidence."):
            match = text
        if match and match not in seen:
            seen.add(match)
            resolved.append(match)
    return resolved
