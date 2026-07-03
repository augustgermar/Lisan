from __future__ import annotations

from datetime import date, timedelta
import re
from pathlib import Path
from typing import Any
from ..utils import listify


SOURCE_TYPES = {
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
}

SENSITIVITY_LEVELS = {"low", "medium", "high", "restricted", "sealed"}
RELIABILITY_LEVELS = {"low", "medium", "high"}
CLAIM_CLASSES = {
    "observation",
    "inference",
    "interpretation",
    "prediction",
    "motive_hypothesis",
    "value_statement",
    "identity_claim",
    "psychological_hypothesis",
}
CLAIM_STATUSES = {"active", "disputed", "confirmed", "rejected", "stale", "superseded"}
CLAIM_OWNERS = {"user", "agent", "external_actor"}
PATTERN_TYPES = {
    "interpretation",
    "emotional_trigger",
    "avoidance_loop",
    "decision_loop",
    "relational_loop",
    "work_loop",
    "authority_response",
    "value_behavior_gap",
    "confidence_evidence_mismatch",
    "identity_claim",
    "psychological_hypothesis",
    "other",
}
PATTERN_STATUSES = {
    "candidate",
    "active_hypothesis",
    "skeptic_reviewed",
    "supported",
    "integrated",
    "disputed",
    "stale",
    "rejected",
    "retired",
    "active",
    "confirmed",
    "superseded",
}
LEGACY_PATTERN_STATUS_MAP = {
    "active": "active_hypothesis",
    "confirmed": "supported",
    "superseded": "retired",
}
BANNED_PATTERN_TERMS = {
    "narcissistic",
    "borderline",
    "autistic",
    "bipolar",
    "trauma disorder",
    "personality disorder",
    "pathological",
    "delusional",
    "paranoid",
}
STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
    "with",
    "in",
    "on",
    "at",
    "by",
    "is",
    "it",
    "as",
    "be",
    "are",
    "was",
    "were",
    "that",
    "this",
    "from",
    "into",
    "when",
    "if",
    "while",
    "may",
    "might",
    "about",
    "than",
    "or",
    "their",
    "my",
    "your",
    "our",
    "his",
    "her",
    "its",
    "them",
    "they",
    "i",
    "we",
    "you",
}
REASONING_ERROR_TAXONOMY = {
    "false_dichotomy",
    "strawman",
    "slippery_slope",
    "ad_hominem",
    "appeal_to_authority",
    "circular_reasoning",
    "hasty_generalization",
    "post_hoc",
    "motte_and_bailey",
    "equivocation",
    "mind_reading",
    "catastrophizing",
    "emotional_reasoning",
    "overgeneralization",
    "personalization",
    "discounting_positives",
    "all_or_nothing_thinking",
    "should_statements",
    "base_rate_neglect",
    "confirmation_bias",
    "availability_bias",
    "survivorship_bias",
    "sunk_cost_fallacy",
    "loss_aversion",
    "status_quo_bias",
    "incentive_misread",
    "insufficient_alternative_hypotheses",
}




def normalize_evidence_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    arena = str(data.get("arena") or data.get("domain_primary") or "cross_arena")
    compartments = listify(data.get("compartments"))
    domain_secondary = listify(data.get("domain_secondary"))
    data.setdefault("type", "evidence")
    data.setdefault("created_at", data.get("created"))
    data.setdefault("created", data.get("created_at"))
    data.setdefault("updated", data.get("created") or data.get("created_at"))
    data.setdefault("source_type", "manual_note")
    data.setdefault("actors", [])
    data.setdefault("sensitivity", "low")
    data.setdefault("reliability", "medium")
    data.setdefault("summary", "")
    data.setdefault("observed_facts", [])
    data.setdefault("linked_claims", [])
    data.setdefault("linked_episodes", [])
    data.setdefault("confidence", str(data.get("reliability", "medium")))
    data["arena"] = arena
    data["domain_primary"] = str(data.get("domain_primary") or arena)
    data["domain_secondary"] = domain_secondary
    data["compartments"] = compartments
    data["actors"] = listify(data.get("actors"))
    data["observed_facts"] = listify(data.get("observed_facts"))
    data["linked_claims"] = listify(data.get("linked_claims"))
    data["linked_episodes"] = listify(data.get("linked_episodes"))
    return data


def normalize_claim_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    arena = str(data.get("arena") or data.get("domain_primary") or "cross_arena")
    compartments = listify(data.get("compartments"))
    domain_secondary = listify(data.get("domain_secondary"))
    data.setdefault("type", "claim")
    data.setdefault("created_at", data.get("created"))
    data.setdefault("created", data.get("created_at"))
    data.setdefault("updated", data.get("created") or data.get("created_at"))
    data.setdefault("claim_text", "")
    data.setdefault("claim_class", "interpretation")
    data.setdefault("owner", "user")
    data.setdefault("status", "active")
    data.setdefault("confidence", 0.5)
    data.setdefault("supporting_evidence", [])
    data.setdefault("contradicting_evidence", [])
    data.setdefault("linked_patterns", [])
    data.setdefault("first_seen", data.get("created_at") or data.get("created"))
    data.setdefault("last_reviewed", data.get("created_at") or data.get("created"))
    data.setdefault("review_notes", "")
    data["arena"] = arena
    data["domain_primary"] = str(data.get("domain_primary") or arena)
    data["domain_secondary"] = domain_secondary
    data["compartments"] = compartments
    data["supporting_evidence"] = listify(data.get("supporting_evidence"))
    data["contradicting_evidence"] = listify(data.get("contradicting_evidence"))
    data["linked_patterns"] = listify(data.get("linked_patterns"))
    return data


def normalize_skeptical_review_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    arena = str(data.get("arena") or data.get("domain_primary") or "cross_arena")
    data.setdefault("type", "skeptical_review")
    data.setdefault("created_at", data.get("created"))
    data.setdefault("created", data.get("created_at"))
    data.setdefault("updated", data.get("created") or data.get("created_at"))
    data.setdefault("summary", "")
    data.setdefault("reviewed_record_id", "")
    data.setdefault("reviewed_record_type", "")
    data.setdefault("approved", False)
    data.setdefault("risk", "medium")
    data.setdefault("recommended_action", "revise")
    data.setdefault("issues", [])
    data.setdefault("priority_questions", [])
    data.setdefault("alternative_hypotheses", [])
    data.setdefault("evidence_needed", [])
    data.setdefault("claim_updates", [])
    data.setdefault("confidence_adjustments", [])
    data.setdefault("reasoning_errors", [])
    data["arena"] = arena
    data["domain_primary"] = str(data.get("domain_primary") or arena)
    data["domain_secondary"] = listify(data.get("domain_secondary"))
    data["issues"] = listify(data.get("issues"))
    data["priority_questions"] = listify(data.get("priority_questions"))
    data["alternative_hypotheses"] = listify(data.get("alternative_hypotheses"))
    data["evidence_needed"] = listify(data.get("evidence_needed"))
    data["reasoning_errors"] = listify(data.get("reasoning_errors"))
    return data


def normalize_pattern_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    arena = str(data.get("arena") or data.get("domain_primary") or "cross_arena")
    compartments = listify(data.get("compartments"))
    domain_secondary = listify(data.get("domain_secondary"))
    data.setdefault("type", "pattern")
    data.setdefault("created_at", data.get("created"))
    data.setdefault("created", data.get("created_at"))
    data.setdefault("updated", data.get("created") or data.get("created_at"))
    data.setdefault("pattern_type", "other")
    data.setdefault("hypothesis", "")
    data.setdefault("supporting_records", [])
    data.setdefault("counterexamples", [])
    data.setdefault("alternative_explanations", [])
    data.setdefault("confidence", 0.35)
    data.setdefault("status", "candidate")
    data.setdefault("first_seen", data.get("created_at") or data.get("created"))
    data.setdefault("last_reviewed", data.get("created_at") or data.get("created"))
    data.setdefault("predictions", [])
    data.setdefault("review_notes", "")
    data.setdefault("evidence_needed", [])
    data.setdefault(
        "counterexample_search",
        {
            "performed": False,
            "search_terms": [],
            "result_summary": "Counterexample search not recorded.",
            "counterexamples": listify(data.get("counterexamples")),
        },
    )
    data.setdefault("strength_override", False)
    data.setdefault(
        "integration_override",
        {
            "enabled": False,
            "reason": "",
            "approved_by": "",
        },
    )
    data["arena"] = arena
    data["domain_primary"] = str(data.get("domain_primary") or arena)
    data["domain_secondary"] = domain_secondary
    data["compartments"] = compartments
    data["supporting_records"] = listify(data.get("supporting_records"))
    data["counterexamples"] = listify(data.get("counterexamples"))
    data["alternative_explanations"] = listify(data.get("alternative_explanations"))
    data["predictions"] = listify(data.get("predictions"))
    data["evidence_needed"] = listify(data.get("evidence_needed"))
    data["status"] = canonical_pattern_status(str(data.get("status") or "candidate"))
    data["counterexample_search"] = normalize_counterexample_search(data.get("counterexample_search"), data["counterexamples"])
    data["integration_override"] = normalize_integration_override(data.get("integration_override"))
    data["strength_override"] = bool(data.get("strength_override", False))
    return data


def canonical_pattern_status(status: str) -> str:
    value = str(status or "candidate").strip()
    return LEGACY_PATTERN_STATUS_MAP.get(value, value if value in PATTERN_STATUSES else "candidate")


def normalize_counterexample_search(value: Any, counterexamples: list[str] | None = None) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    counterexamples = listify(counterexamples)
    performed = bool(data.get("performed", False))
    search_terms = listify(data.get("search_terms"))
    result_summary = str(data.get("result_summary") or "")
    if not result_summary:
        result_summary = "Counterexample search performed." if performed else "Counterexample search not recorded."
    return {
        "performed": performed,
        "search_terms": search_terms,
        "result_summary": result_summary,
        "counterexamples": listify(data.get("counterexamples")) or counterexamples,
    }


def normalize_integration_override(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    return {
        "enabled": bool(data.get("enabled", False)),
        "reason": str(data.get("reason") or ""),
        "approved_by": str(data.get("approved_by") or ""),
    }


def pattern_minimum_age_days(frontmatter: dict[str, Any]) -> int:
    if str(frontmatter.get("pattern_type")) in {"identity_claim", "psychological_hypothesis"}:
        return 30
    return 7


def pattern_age_days(frontmatter: dict[str, Any], as_of: date | None = None) -> int | None:
    as_of = as_of or date.today()
    created = str(frontmatter.get("created") or frontmatter.get("created_at") or "")
    if not created:
        return None
    try:
        created_date = date.fromisoformat(created)
    except ValueError:
        return None
    return max(0, (as_of - created_date).days)


def pattern_support_count(frontmatter: dict[str, Any]) -> int:
    return len(listify(frontmatter.get("supporting_records")))


def pattern_hypothesis_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9][a-z0-9_-]+", text.lower()) if token not in STOPWORDS and len(token) > 2}


def pattern_similarity_score(left: str, right: str) -> float:
    left_tokens = pattern_hypothesis_tokens(left)
    right_tokens = pattern_hypothesis_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return overlap / union if union else 0.0


def pattern_contains_diagnostic_language(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in BANNED_PATTERN_TERMS)


def pattern_is_too_broad(text: str) -> bool:
    lowered = text.lower()
    broad_markers = {
        "always",
        "never",
        "everyone",
        "no one",
        "everything",
        "all the time",
        "nothing but",
        "only reason",
        "the only reason",
        "all of",
    }
    if any(marker in lowered for marker in broad_markers):
        return True
    return len(pattern_hypothesis_tokens(text)) < 4


def load_existing_patterns(vault: Path) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    patterns_root = vault / "patterns"
    if not patterns_root.exists():
        return existing
    for path in sorted(patterns_root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = normalize_pattern_frontmatter(doc.frontmatter)
        fm["_path"] = str(path)
        existing.append(fm)
    return existing


def pattern_conflicts_with_existing(hypothesis: str, pattern_type: str, existing_patterns: list[dict[str, Any]], similarity_threshold: float = 0.55) -> bool:
    for pattern in existing_patterns:
        status = canonical_pattern_status(str(pattern.get("status") or "candidate"))
        if status in {"disputed", "stale", "rejected", "retired"}:
            continue
        if str(pattern.get("pattern_type") or "") not in {pattern_type, "other", ""} and pattern_type not in {"other"}:
            continue
        score = pattern_similarity_score(hypothesis, str(pattern.get("hypothesis") or pattern.get("summary") or ""))
        if score >= similarity_threshold:
            return True
    return False


def pattern_counterexample_search_result(
    bundle_text: str,
    hypothesis: str,
    pattern_type: str,
    supporting_records: list[str] | None = None,
) -> dict[str, Any]:
    supporting_records = listify(supporting_records)
    search_terms = sorted(pattern_hypothesis_tokens(hypothesis) | {pattern_type})
    counterexamples = _extract_counterexample_lines(bundle_text, pattern_type)
    performed = True
    result_summary = "Counterexample search completed."
    if counterexamples == ["No explicit counterexamples found in the scanned records."]:
        result_summary = "No explicit counterexamples found in the scanned records."
    else:
        result_summary = f"Found {len(counterexamples)} possible counterexample signal(s) while scanning the bundle."
    return {
        "performed": performed,
        "search_terms": search_terms[:10],
        "result_summary": result_summary,
        "counterexamples": counterexamples[:5],
        "supporting_records": supporting_records[:],
    }


def classify_reasoning_errors(text: str) -> list[str]:
    lowered = text.lower()
    errors: list[str] = []
    patterns = {
        "mind_reading": ["mind reading", "you are thinking", "they are thinking", "he was trying to"],
        "catastrophizing": ["disaster", "ruined", "destroyed", "worst case", "everything will fall apart"],
        "emotional_reasoning": ["i feel like", "feels like therefore", "because it feels", "therefore it's true"],
        "overgeneralization": ["always", "never", "everyone", "no one", "every time"],
        "personalization": ["because of me", "my fault", "they targeted me"],
        "discounting_positives": ["doesn't count", "just lucky", "nothing good", "ignore the good"],
        "all_or_nothing_thinking": ["either", "totally", "completely", "100%", "all or nothing"],
        "should_statements": ["should have", "shouldn't have", "must have"],
        "confirmation_bias": ["obviously", "as expected", "proves my point"],
        "insufficient_alternative_hypotheses": ["only explanation", "the only reason", "must be because"],
        "post_hoc": ["after that, so because", "therefore caused"],
        "motte_and_bailey": ["motte", "bailey"],
        "equivocation": ["means the same", "same thing", "therefore"],
        "hasty_generalization": ["always", "never", "this proves"],
        "false_dichotomy": ["either/or", "only two choices"],
        "slippery_slope": ["if this happens then everything"],
        "ad_hominem": ["because they are", "they're a"],
        "appeal_to_authority": ["expert says", "because authority"],
        "circular_reasoning": ["because it is true", "it is true because"],
        "base_rate_neglect": ["unique", "special case", "won't happen here"],
        "availability_bias": ["recently", "memorable", "stands out"],
        "survivorship_bias": ["success stories", "winners", "only the people who"],
        "sunk_cost_fallacy": ["already invested", "can't stop now"],
        "loss_aversion": ["can't lose", "afraid of losing"],
        "status_quo_bias": ["keep things as they are", "don't change"],
        "incentive_misread": ["must be trying to", "their incentive is"],
    }
    for code, tokens in patterns.items():
        if any(token in lowered for token in tokens):
            errors.append(code)
    return list(dict.fromkeys(errors))


def review_claim_against_evidence(claim_text: str, evidence_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    evidence_items = evidence_items or []
    lowered = claim_text.lower()
    observed_facts = []
    interpretations = [claim_text]
    alternative_hypotheses: list[str] = []
    evidence_needed: list[str] = []
    supporting = []
    contradicting = []
    reasoning_errors = classify_reasoning_errors(claim_text)
    confidence_delta = 0.0

    if any(token in lowered for token in ("scapegoat", "hostile", "sabotage", "malicious", "targeted", "punish")):
        reasoning_errors.append("mind_reading")
        alternative_hypotheses.extend([
            "The actor may have been delegating work rather than signaling hostility.",
            "The actor may have lacked technical context and asked for a normal escalation path.",
            "The interaction may reflect routine coordination rather than intent to harm.",
        ])
        evidence_needed.extend([
            "Direct wording from the relevant artifact or message.",
            "Prior pattern showing similar behavior across multiple instances.",
        ])
        confidence_delta -= 0.2

    if not evidence_items:
        evidence_needed.append("An external artifact or direct observation that bears on the claim.")
        reasoning_errors.append("insufficient_alternative_hypotheses")
        confidence_delta -= 0.2

    for item in evidence_items:
        summary = str(item.get("summary") or item.get("claim_text") or "").strip()
        observed = listify(item.get("observed_facts"))
        if observed:
            observed_facts.extend(observed)
        if summary:
            if any(token in summary.lower() for token in ("asked", "said", "requested", "noted", "scheduled", "confirmed")):
                supporting.append(str(item.get("id") or summary))
                confidence_delta += 0.15
            if any(token in summary.lower() for token in ("did not", "never", "no evidence", "contradict", "disagree")):
                contradicting.append(str(item.get("id") or summary))
                confidence_delta -= 0.15

    if supporting:
        evidence_needed = evidence_needed[:1]
    if not alternative_hypotheses:
        alternative_hypotheses.append("The claim may be a tentative interpretation rather than a supported fact.")

    if not observed_facts:
        observed_facts.append("No directly observed facts were extracted from the available evidence set.")

    confidence = max(0.0, min(1.0, 0.5 + confidence_delta))
    status = "disputed" if contradicting or confidence < 0.45 else ("confirmed" if confidence >= 0.75 and supporting else "active")
    recommended_action = "hold" if confidence < 0.35 else ("revise" if contradicting or not supporting else "approve")

    return {
        "observed_facts": observed_facts,
        "interpretations": interpretations,
        "alternative_hypotheses": alternative_hypotheses[:5],
        "evidence_needed": evidence_needed[:5],
        "confidence": confidence,
        "status": status,
        "recommended_action": recommended_action,
        "reasoning_errors": list(dict.fromkeys(reasoning_errors)),
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
    }


def review_pattern_against_records(hypothesis: str, supporting_records: list[str] | None = None, counterexamples: list[str] | None = None) -> dict[str, Any]:
    supporting_records = listify(supporting_records)
    counterexamples = listify(counterexamples)
    lowered = hypothesis.lower()
    issue_tags = classify_reasoning_errors(hypothesis)
    alternative_explanations = [
        "The apparent pattern may reflect a small sample rather than a stable longitudinal tendency.",
        "The pattern may be driven by context-specific events rather than a general recurring loop.",
    ]
    if any(token in lowered for token in ("avoid", "avoidance", "delay", "procrast", "defer")):
        issue_tags.append("insufficient_alternative_hypotheses")
    if pattern_contains_diagnostic_language(hypothesis):
        issue_tags.append("pathologizing_language")
        alternative_explanations.append("The wording may be pathologizing where a non-diagnostic explanation fits better.")
    support_count = len(supporting_records)
    actual_counterexamples = [item for item in counterexamples if "no explicit counterexamples" not in item.lower() and "none" not in item.lower()]
    confidence = 0.25 + min(0.45, 0.13 * support_count)
    if support_count < 2:
        confidence -= 0.12
    if actual_counterexamples:
        confidence -= 0.12
    elif support_count >= 3:
        confidence += 0.08
    if pattern_is_too_broad(hypothesis):
        confidence -= 0.1
    if pattern_contains_diagnostic_language(hypothesis):
        confidence -= 0.35
    confidence = max(0.0, min(1.0, confidence))
    counterexample_search = {
        "performed": True,
        "search_terms": sorted(pattern_hypothesis_tokens(hypothesis))[:10],
        "result_summary": "Counterexample search performed.",
        "counterexamples": counterexamples[:5] or ["No explicit counterexamples found in the scanned records."],
    }
    approved = confidence >= 0.55 and support_count >= 2 and not pattern_contains_diagnostic_language(hypothesis) and not pattern_is_too_broad(hypothesis)
    approved_for_dreamer = approved and support_count >= 3 and confidence >= 0.65 and bool(counterexample_search.get("performed")) and len(alternative_explanations) >= 1
    if pattern_contains_diagnostic_language(hypothesis):
        approved = False
        approved_for_dreamer = False
    if support_count >= 3 and confidence >= 0.75 and not actual_counterexamples:
        status = "supported"
    elif approved:
        status = "skeptic_reviewed"
    elif actual_counterexamples or confidence < 0.45:
        status = "disputed"
    elif support_count == 0:
        status = "candidate"
    else:
        status = "active_hypothesis"
    evidence_needed = [
        "A later episode or artifact showing the same pattern under similar conditions.",
        "A clear counterexample or disconfirming case to test the boundary of the pattern.",
    ]
    return {
        "supporting_records": supporting_records,
        "counterexamples": counterexamples or ["No explicit counterexamples found in the scanned records."],
        "alternative_explanations": alternative_explanations[:3],
        "confidence": confidence,
        "status": status,
        "predictions": [
            "If the hypothesis is correct, similar cues should recur in future episodes.",
        ],
        "review_notes": "Pattern hypothesis should remain provisional and longitudinally testable.",
        "evidence_needed": evidence_needed,
        "reasoning_errors": list(dict.fromkeys(issue_tags)),
        "approved": approved,
        "approved_for_dreamer": approved_for_dreamer,
        "counterexample_search": counterexample_search,
    }


def discover_pattern_hypotheses(bundle_text: str) -> list[dict[str, Any]]:
    lowered = bundle_text.lower()
    patterns: list[dict[str, Any]] = []
    candidates = [
        ("avoidance_loop", ["avoid", "delay", "procrast", "defer", "put off"], "The narrative repeatedly returns to avoidance or deferral under pressure."),
        ("decision_loop", ["decide", "decision", "revisit", "uncertain"], "The narrative repeatedly circles the same decision without closure."),
        ("relational_loop", ["relationship", "partner", "friend", "family", "argue", "conflict"], "Relational dynamics recur as a stable loop."),
        ("work_loop", ["work", "project", "deadline", "manager", "meeting"], "Work-related concerns recur across records."),
        ("authority_response", ["manager", "boss", "authority", "leadership", "supervisor"], "Authority-related cues trigger a predictable response."),
        ("value_behavior_gap", ["should", "value", "want", "wish", "but", "however"], "Stated values and observed behavior may diverge."),
        ("confidence_evidence_mismatch", ["confidence", "certain", "sure", "evidence", "proof"], "Confidence level may be misaligned with the amount of evidence."),
    ]
    for pattern_type, tokens, hypothesis in candidates:
        hits = sum(1 for token in tokens if token in lowered)
        if hits == 0:
            continue
        support = _extract_record_ids(bundle_text, tokens)
        if len(support) < 2:
            continue
        if pattern_is_too_broad(hypothesis) or pattern_contains_diagnostic_language(hypothesis):
            continue
        counterexamples = _extract_counterexample_lines(bundle_text, pattern_type)
        review = review_pattern_against_records(hypothesis, support, counterexamples)
        patterns.append(
            {
                "pattern_type": pattern_type,
                "hypothesis": hypothesis,
                "supporting_records": support,
                "counterexamples": review["counterexamples"],
                "alternative_explanations": review["alternative_explanations"],
                "confidence": review["confidence"],
                "status": review["status"],
                "first_seen": today(),
                "last_reviewed": today(),
                "predictions": review["predictions"],
                "review_notes": review["review_notes"],
                "evidence_needed": review["evidence_needed"],
                "counterexample_search": review["counterexample_search"],
                "strength_override": False,
                "integration_override": {"enabled": False, "reason": "", "approved_by": ""},
            }
        )
    return patterns[:5]


def _extract_record_ids(bundle_text: str, tokens: list[str]) -> list[str]:
    ids: list[str] = []
    current_record_id = ""
    record_buffer: list[str] = []

    def _flush() -> None:
        nonlocal ids, current_record_id, record_buffer
        if current_record_id and any(token in "\n".join(record_buffer).lower() for token in tokens):
            ids.append(current_record_id)
        record_buffer = []

    for line in bundle_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            _flush()
            current_record_id = stripped[4:].strip()
            continue
        if not stripped.startswith("- `"):
            if current_record_id:
                record_buffer.append(stripped)
            continue
        parts = stripped.split("`")
        if len(parts) >= 3:
            record_id = parts[1].strip()
            haystack = stripped.lower()
            if any(token in haystack for token in tokens):
                ids.append(record_id)
    _flush()
    return list(dict.fromkeys(ids))


def _extract_counterexample_lines(bundle_text: str, pattern_type: str) -> list[str]:
    lines = []
    for line in bundle_text.splitlines():
        lowered = line.lower()
        stripped = line.strip()
        if stripped.startswith("## "):
            continue
        if stripped.startswith("- ") and (
            "contradict" in lowered or "counterexample" in lowered or "dispute" in lowered or "not supported" in lowered
        ):
            lines.append(stripped)
    if not lines:
        lines.append("No explicit counterexamples found in the scanned records.")
    return lines[:5]


def today() -> str:
    return date.today().isoformat()
