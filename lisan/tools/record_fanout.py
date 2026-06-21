from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import slugify, today_iso
from .epistemic import listify
from .log import log_error
from .reference_resolution import normalize_text, resolve_reference
from .record_factory import (
    CreatedRecord,
    STATE_TTLS,
    new_claim,
    new_decision,
    new_evidence,
    new_open_loop,
    normalize_state_category,
    upsert_state,
)
from .rebuild_index import index_single_record


# ── Reference resolution (claim IDs) ─────────────────────────────────────────

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


# ── Reference resolution (evidence IDs) ──────────────────────────────────────
#
# The writer often produces claim/evidence link strings that are natural-language
# titles ("Transcript note: staffing reflection") rather than resolvable IDs.
# We mirror the claim-id resolution pattern: build a map from every stringified
# form of an evidence entry's title to the generated evidence ID, then rewrite
# incoming link arrays through that map. Unresolvable strings are dropped
# silently so the vault validator stays clean.

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


# ── Shared utilities ──────────────────────────────────────────────────────────

def merge_links(*sources: Any) -> list[str]:
    """Flatten and deduplicate link sources for a record's `links` frontmatter field."""
    out: list[str] = []
    seen: set[str] = set()
    for src in sources:
        for item in listify(src):
            value = str(item).strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def basis_or_default(entry: Any, default: str) -> str:
    """Pull per-record confidence_basis from the writer; fall back only when missing."""
    if isinstance(entry, dict):
        explicit = str(entry.get("confidence_basis") or "").strip()
        if explicit:
            return explicit
    return default


def _record_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
        elif value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts).strip()


def _load_records_by_type(vault: Path, folder: str, record_type: str) -> list[tuple[Path, dict[str, Any]]]:
    root = vault / folder
    if not root.exists():
        return []
    docs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("type") or "") != record_type:
            continue
        docs.append((path, dict(doc.frontmatter)))
    return docs


def _update_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm.update(updates)
    fm["updated"] = today_iso()
    write_markdown(path, fm, doc.body)


def disclosure_or_default(entry: Any, writer: dict[str, Any] | None = None) -> str:
    if isinstance(entry, dict):
        explicit = str(entry.get("disclosure") or "").strip()
        if explicit:
            return explicit
    if writer:
        frontmatter = writer.get("frontmatter")
        if isinstance(frontmatter, dict):
            explicit = str(frontmatter.get("disclosure") or "").strip()
            if explicit:
                return explicit
    return "private"


def index_created_record(vault: Path, record: CreatedRecord | None, conn: sqlite3.Connection | None) -> None:
    if conn is None or record is None or not record.created:
        return
    index_single_record(record.path, vault, conn)


# ── Domain inference ──────────────────────────────────────────────────────────

_RELATIONAL_TERMS = (
    "mom", "dad", "mother", "father", "wife", "husband", "spouse",
    "partner", "sister", "brother", "daughter", "son", "kid", "kids",
    "child", "children", "family", "parent", "co-parent", "ex",
)
_WORK_TERMS = (
    "manager", "boss", "team", "co-worker", "coworker", "colleague",
    "employee", "employer", "client", "customer", "vendor", "project",
    "meeting", "deadline", "salary", "promotion", "office", "work",
    "job", "career", "performance", "review", "contract", "hire",
    "fired", "layoff", "startup", "company", "business",
)


def _infer_domain(
    explicit: str | None,
    fallback: str,
    *,
    text: str | None = None,
    entity_names: list[str] | None = None,
) -> str:
    valid = set(STATE_TTLS.keys()) | {"cross_arena"}
    explicit_clean = (explicit or "").strip().lower()
    if explicit_clean in valid and explicit_clean != "cross_arena":
        return explicit_clean
    haystack_parts: list[str] = []
    if text:
        haystack_parts.append(text)
    if entity_names:
        haystack_parts.append(" ".join(entity_names))
    haystack = " ".join(haystack_parts).lower()
    if haystack:
        if any(term in haystack for term in _RELATIONAL_TERMS):
            return "relational"
        if any(term in haystack for term in _WORK_TERMS):
            return "work"
    if explicit_clean in valid:
        return explicit_clean
    return fallback


def _entity_canonical_names(writer: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for entry in writer.get("entities_to_create") or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                names.append(name)
    return names


_PRONOUN_RE = re.compile(r"\b(she|he|they|her|him|them)\b", re.IGNORECASE)


def _resolve_pronouns(summary: str, entity_names: list[str]) -> str:
    if not summary or not entity_names:
        return summary
    if len(entity_names) != 1:
        return summary
    name = entity_names[0]

    def repl(match: re.Match[str]) -> str:
        pronoun = match.group(0)
        start = match.start()
        prefix = summary[:start].rstrip()
        if prefix and not prefix.endswith((".", "!", "?")):
            return pronoun
        return name if pronoun[0].isupper() else name.lower()

    return _PRONOUN_RE.sub(repl, summary)


# ── Negation guard for decisions ──────────────────────────────────────────────

_NEGATION_PREFIXES = (
    "i have not", "i haven't", "i hadn't", "i did not", "i didn't",
    "i am not", "i'm not", "i was not", "i wasn't",
    "not sure", "not decided", "haven't decided", "haven't done",
    "did not", "didn't", "has not", "hasn't",
    "no decision", "not yet", "i've not",
    # Pronoun-stripped forms (writer often drops "I" from decision titles)
    "have not", "had not", "not done", "not completed",
)


def _is_negated_decision(title: str, summary: str) -> bool:
    combined = f"{title} {summary}".lower()
    return any(phrase in combined for phrase in _NEGATION_PREFIXES)


# ── Shared fanout: open loops ─────────────────────────────────────────────────

def fanout_open_loops(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    source_text: str = "",
    index_conn: sqlite3.Connection | None = None,
) -> None:
    """Materialize open loops immediately — open loops are always capture_now per spec.

    Skips any loop whose `owner` is not the user. The writer prompt asks for
    explicit ownership; this filter is a backstop so other people's pending
    questions never become user-owned todos.
    """
    _close_matching_open_loops(vault, source_text or str(writer.get("summary") or ""), draft_rel=draft_rel)
    loops = writer.get("open_loops_to_create") or []
    entity_names = _entity_canonical_names(writer)
    for loop in loops:
        if not isinstance(loop, dict):
            continue
        title = str(loop.get("title") or "").strip()
        next_action = str(loop.get("next_action") or "").strip()
        summary = str(loop.get("summary") or "").strip()
        priority = str(loop.get("priority") or "medium").strip()
        owner = str(loop.get("owner") or "user").strip().lower()
        explicit_domain = str(loop.get("domain", loop.get("arena")) or "").strip()
        domain = _infer_domain(
            explicit_domain,
            fallback="cross_arena",
            text=f"{title} {summary} {next_action}",
            entity_names=entity_names,
        )
        if not title or not next_action:
            continue
        if owner not in {"user", "self", "me", ""}:
            continue
        if priority not in ("low", "medium", "high"):
            priority = "medium"
        try:
            created = new_open_loop(
                vault=vault,
                title=title,
                domain_primary=domain,
                summary=summary or title,
                next_action=next_action,
                priority=priority,
                confidence="low",
                confidence_basis=basis_or_default(loop, "Auto-extracted from conversation"),
                disclosure=disclosure_or_default(loop, writer),
                links=merge_links(loop.get("linked_claims"), loop.get("linked_episodes"), [draft_rel]),
            )
            index_created_record(vault, created, index_conn)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "fanout.open_loop", exc)


_OPEN_LOOP_COMPLETION_MARKERS = (
    "done",
    "finished",
    "completed",
    "closed",
    "resolved",
    "taken care of",
    "took care of",
    "sent",
    "emailed",
    "told",
    "informed",
    "called",
    "updated",
    "handled",
    "fixed",
)


def _looks_like_completion(text: str) -> bool:
    lowered = normalize_text(text)
    return any(marker in lowered for marker in _OPEN_LOOP_COMPLETION_MARKERS)


def _close_matching_open_loops(vault: Path, source_text: str, *, draft_rel: str) -> None:
    if not source_text or not _looks_like_completion(source_text):
        return
    candidates = []
    for path, fm in _load_records_by_type(vault, "open_loops", "open_loop"):
        if str(fm.get("status") or "") != "active":
            continue
        text = _record_text(fm.get("summary"), fm.get("next_action"), fm.get("title"), fm.get("owner"), fm.get("blocked_by"))
        candidates.append({"path": path, "id": fm.get("id"), "summary": text, "title": fm.get("title"), "next_action": fm.get("next_action"), "owner": fm.get("owner")})
    if not candidates:
        return
    result = resolve_reference(source_text, candidates)
    if result.candidate is None or result.confidence < 0.4:
        return
    path = Path(str(result.candidate["path"]))
    resolved_note = f"Resolved by {draft_rel}"
    _update_frontmatter(
        path,
        {
            "status": "resolved",
            "resolved_by": draft_rel,
            "resolved_note": resolved_note,
            "resolved_at": today_iso(),
            "links": merge_links(load_markdown(path).frontmatter.get("links"), [draft_rel]),
        },
    )


# ── Shared fanout: decisions ──────────────────────────────────────────────────

def fanout_decisions(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str | None = None,
    source_text: str = "",
    index_conn: sqlite3.Connection | None = None,
) -> None:
    decisions = list(writer.get("decisions_to_create") or [])
    entity_names = _entity_canonical_names(writer)
    # Extraction fallback: when the writer emits record_type=decision at top level
    # with no decisions_to_create list, synthesize from the summary.
    if not decisions and str(writer.get("record_type") or "").strip().lower() == "decision":
        summary = str(writer.get("summary") or "").strip()
        if summary:
            _raw_fm = writer.get("frontmatter")
            frontmatter: dict[str, Any] = _raw_fm if isinstance(_raw_fm, dict) else {}
            decisions = [{
                "title": summary,
                "summary": summary,
                "domain": frontmatter.get("domain_primary") or frontmatter.get("domain") or "cross_arena",
                "significance": writer.get("significance") or "medium",
                "alternatives_considered": listify(frontmatter.get("alternatives_considered")),
                "revisit_conditions": listify(frontmatter.get("revisit_conditions")),
                "confidence_basis": frontmatter.get("confidence_basis"),
                "linked_episodes": listify(frontmatter.get("linked_episodes")),
                "linked_claims": listify(frontmatter.get("linked_claims")),
            }]
    for entry in decisions:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if not title or not summary:
            continue
        if _is_negated_decision(title, summary):
            continue
        domain = _infer_domain(
            str(entry.get("domain", entry.get("arena")) or ""),
            fallback="cross_arena",
            text=f"{title} {summary}",
            entity_names=entity_names,
        )
        significance = str(entry.get("significance") or "low").strip()
        alternatives = listify(entry.get("alternatives_considered"))
        revisit = listify(entry.get("revisit_conditions"))
        if significance not in ("low", "medium", "high"):
            significance = "low"
        supersedes = _supersede_matching_decisions(
            vault=vault,
            title=title,
            summary=summary,
            source_text=source_text or str(writer.get("summary") or ""),
        )
        try:
            created = new_decision(
                vault=vault,
                title=title,
                domain_primary=domain,
                summary=summary,
                significance=significance,
                confidence="low",
                confidence_basis=basis_or_default(entry, "Auto-extracted from conversation"),
                alternatives_considered=alternatives,
                revisit_conditions=revisit,
                supersedes=supersedes,
                disclosure=disclosure_or_default(entry, writer),
                links=merge_links(
                    entry.get("linked_claims"),
                    entry.get("linked_episodes"),
                    [draft_rel] if draft_rel else [],
                    supersedes,
                ),
            )
            index_created_record(vault, created, index_conn)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "fanout.decision", exc)


_DECISION_REVERSAL_MARKERS = (
    "changed my mind",
    "change my mind",
    "instead",
    "switch to",
    "switched to",
    "no longer",
    "won't",
    "will not",
    "not anymore",
    "replace",
    "replacing",
    "reverse",
    "revise",
    "update the decision",
)


def _looks_like_decision_reversal(text: str) -> bool:
    lowered = normalize_text(text)
    return any(marker in lowered for marker in _DECISION_REVERSAL_MARKERS)


def _supersede_matching_decisions(
    *,
    vault: Path,
    title: str,
    summary: str,
    source_text: str,
) -> list[str]:
    if not _looks_like_decision_reversal(f"{title} {summary} {source_text}"):
        return []
    candidates = []
    for path, fm in _load_records_by_type(vault, "decisions", "decision"):
        if str(fm.get("status") or "") != "active":
            continue
        candidate_summary = _record_text(fm.get("title"), fm.get("summary"), fm.get("links"), fm.get("alternatives_considered"))
        candidates.append(
            {
                "path": path,
                "id": fm.get("id"),
                "title": fm.get("summary") or fm.get("title"),
                "summary": candidate_summary,
                "links": fm.get("links"),
            }
        )
    if not candidates:
        return []
    result = resolve_reference(f"{title} {summary} {source_text}", candidates)
    if result.candidate is None or result.confidence < 0.4:
        return []
    path = Path(str(result.candidate["path"]))
    old_id = str(result.candidate.get("id") or path.stem)
    _update_frontmatter(
        path,
        {
            "status": "superseded",
            "superseded_by": f"decision.{slugify(title)}",
            "links": merge_links(load_markdown(path).frontmatter.get("links"), [f"decision.{slugify(title)}"]),
        },
    )
    return [old_id]


# ── Shared fanout: evidence ───────────────────────────────────────────────────

def fanout_evidence(
    vault: Path,
    writer: dict[str, Any],
    transcript_path: Path,
    draft_rel: str,
    index_conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    """Create evidence records and return a title→evidence_id map.

    Evidence is materialized BEFORE claims so the claim-creation step can
    resolve `supporting_evidence` strings expressed as evidence titles (Finding 4).
    """
    evidence_items = writer.get("evidence_to_create") or []
    transcript_rel = str(transcript_path.relative_to(vault))
    entity_names = _entity_canonical_names(writer)
    evidence_id_map: dict[str, str] = {}
    for entry in evidence_items:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("summary") or "").strip()
        if not title:
            continue
        arena = _infer_domain(
            str(entry.get("arena") or ""),
            fallback="cross_arena",
            text=f"{title} {entry.get('summary') or ''}",
            entity_names=entity_names,
        )
        try:
            created = new_evidence(
                vault=vault,
                title=title,
                source_type=str(entry.get("source_type") or "manual_note").strip(),
                source_uri=str(entry.get("source_uri") or transcript_rel),
                artifact_ref=str(entry.get("artifact_ref") or transcript_rel),
                artifact_hash=str(entry.get("artifact_hash") or "").strip() or None,
                timestamp_of_artifact=str(entry.get("timestamp_of_artifact") or "").strip() or None,
                actors=listify(entry.get("actors")),
                arena=arena,
                sensitivity=str(entry.get("sensitivity") or "low").strip(),
                reliability=str(entry.get("reliability") or "medium").strip(),
                summary=str(entry.get("summary") or title),
                observed_facts=listify(entry.get("observed_facts")),
                verbatim_excerpt=str(entry.get("verbatim_excerpt") or "").strip() or None,
                # Claims haven't been created yet; store raw writer-supplied link
                # strings and let rebuild-index resolve them later.
                linked_claims=listify(entry.get("linked_claims")),
                linked_episodes=merge_links(entry.get("linked_episodes"), [draft_rel]),
                confidence_basis=basis_or_default(entry, "Auto-extracted from conversation"),
                disclosure=disclosure_or_default(entry, writer),
            )
            evidence_doc = load_markdown(created.path)
            evidence_id = str(evidence_doc.frontmatter.get("id") or "")
            if evidence_id:
                register_evidence_reference(evidence_id_map, entry, evidence_id)
            index_created_record(vault, created, index_conn)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "fanout.evidence", exc)
    return evidence_id_map


# ── Shared fanout: claims ─────────────────────────────────────────────────────

def fanout_claims(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str,
    db_path: Path | None = None,
    evidence_id_map: dict[str, str] | None = None,
    index_conn: sqlite3.Connection | None = None,
) -> dict[str, str]:
    claims = writer.get("claims_to_create") or []
    claim_id_map: dict[str, str] = {}
    entity_names = _entity_canonical_names(writer)
    for entry in claims:
        if not isinstance(entry, dict):
            continue
        claim_text = str(entry.get("claim_text") or entry.get("summary") or "").strip()
        if not claim_text:
            continue
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        arena = _infer_domain(
            str(entry.get("arena") or ""),
            fallback="cross_arena",
            text=claim_text,
            entity_names=entity_names,
        )
        # Rewrite writer-supplied evidence titles into evidence IDs so claim
        # links resolve under validation (Finding 4). Keep ONLY entries that
        # resolve to a real evidence ID (or are already an `evidence.` id);
        # unresolvable natural-language prose is dropped rather than stored as a
        # dangling link target ("Mara's statement", "user_reported_context",
        # "Bram's recent actions"). The previous `or listify(...)` fallback
        # re-introduced that prose whenever nothing resolved — the bug this fixes.
        supporting = resolve_evidence_links(
            listify(entry.get("supporting_evidence")), evidence_id_map or {},
        )
        contradicting = resolve_evidence_links(
            listify(entry.get("contradicting_evidence")), evidence_id_map or {},
        )
        try:
            created = new_claim(
                vault=vault,
                claim_text=claim_text,
                claim_class=str(entry.get("claim_class") or "interpretation").strip(),
                owner=str(entry.get("owner") or "user").strip(),
                status=str(entry.get("status") or "active").strip(),
                confidence=confidence,
                supporting_evidence=supporting,
                contradicting_evidence=contradicting,
                linked_patterns=listify(entry.get("linked_patterns")),
                first_seen=str(entry.get("first_seen") or "").strip() or None,
                last_reviewed=str(entry.get("last_reviewed") or "").strip() or None,
                review_notes=str(entry.get("review_notes") or "").strip(),
                arena=arena,
                privacy=str(entry.get("privacy") or "personal").strip(),
                disclosure=disclosure_or_default(entry, writer),
                significance=str(entry.get("significance") or "low").strip(),
                summary=str(entry.get("summary") or claim_text[:120]),
                confidence_basis=basis_or_default(
                    entry, "Claim confidence assessed from supporting and contradicting evidence",
                ),
            )
            claim_doc = load_markdown(created.path)
            claim_id = str(claim_doc.frontmatter.get("id") or "")
            if claim_id:
                register_claim_reference(claim_id_map, entry, claim_id)
                _link_claim_to_draft(vault=vault, claim_id=claim_id, draft_rel=draft_rel)
                index_created_record(vault, created, index_conn)
        except FileExistsError:
            pass
        except Exception as exc:
            log_error(vault, "fanout.claim", exc)
    return claim_id_map


def _link_claim_to_draft(*, vault: Path, claim_id: str, draft_rel: str) -> None:
    """Ensure the claim's `links` frontmatter records the draft origin."""
    if not draft_rel:
        return
    try:
        for path in (vault / "claims").glob("*.md"):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            if str(doc.frontmatter.get("id") or "") != claim_id:
                continue
            fm = dict(doc.frontmatter)
            fm["links"] = merge_links(fm.get("links"), [draft_rel])
            write_markdown(path, fm, doc.body)
            return
    except Exception as exc:
        log_error(vault, "fanout.claim.link", exc)


# ── Shared fanout: state updates ──────────────────────────────────────────────

def fanout_state_updates(
    vault: Path,
    writer: dict[str, Any],
    draft_rel: str | None = None,
    index_conn: sqlite3.Connection | None = None,
) -> None:
    updates = writer.get("state_updates") or []
    entity_names = _entity_canonical_names(writer)
    for update in updates:
        if not isinstance(update, dict):
            continue
        raw_category = update.get("category", update.get("arena"))
        summary = str(update.get("summary") or "").strip()
        confidence = str(update.get("confidence") or "low").strip()
        inferred = _infer_domain(
            str(raw_category or ""),
            fallback="",
            text=summary,
            entity_names=entity_names,
        )
        state_category = normalize_state_category(inferred or raw_category, summary=summary)
        if not state_category or not summary or state_category not in STATE_TTLS:
            continue
        if confidence not in ("low", "medium", "high"):
            confidence = "low"
        summary = _resolve_pronouns(summary, entity_names)
        sources: list[Any] = [draft_rel] if draft_rel else []
        try:
            created = upsert_state(
                vault=vault,
                state_category=state_category,
                summary=summary,
                confidence=confidence,
                confidence_basis=basis_or_default(update, "Auto-extracted from conversation"),
                sources=merge_links(update.get("sources"), sources),
                disclosure=disclosure_or_default(update, writer),
            )
            index_created_record(vault, created, index_conn)
        except Exception as exc:
            log_error(vault, "fanout.state_update", exc)
