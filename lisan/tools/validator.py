from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from .db import connect as _db_connect

from ..config import load_config
from ..frontmatter import FrontmatterError, load_markdown
from ..paths import vault_root, schemas_dir, repo_root
from ..schemas import load_schemas
from .domain_fields import normalize_domain_fields
from .common import iter_markdown_files, parse_date


@dataclass(slots=True)
class ValidationIssue:
    path: Path
    message: str
    severity: str = "error"


@dataclass(slots=True)
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def add(self, path: Path, message: str, severity: str = "error") -> None:
        self.issues.append(ValidationIssue(path=path, message=message, severity=severity))

    def summary(self) -> str:
        errors = sum(1 for issue in self.issues if issue.severity == "error")
        warnings = sum(1 for issue in self.issues if issue.severity == "warning")
        return f"{errors} error(s), {warnings} warning(s)"


UNIVERSAL_REQUIRED = {
    "id",
    "type",
    "created",
    "updated",
    "status",
    "significance",
    "domain_primary",
    "domain_secondary",
    "privacy",
    "summary",
    "links",
    "confidence",
    "confidence_basis",
    "last_confirmed",
    "review_after",
}

TYPE_FIELDS = {
    "entity": {"subtype", "canonical_name", "aliases", "nickname", "disambiguation", "epoch", "epoch_started", "previous_epochs"},
    "episode": {"entities", "evidence", "claims", "source"},
    "knowledge": set(),
    "artifact": {
        "source_type",
        "source_path",
        "artifact_hash",
        "file_name",
        "file_ext",
        "size_bytes",
        "modified_at",
        "imported_at",
        "ingestion_status",
        "sensitivity",
        "linked_evidence",
        "linked_claims",
    },
    "evidence": {
        "source_type",
        "actors",
        "sensitivity",
        "reliability",
        "observed_facts",
        "linked_claims",
        "linked_episodes",
    },
    "claim": {
        "claim_text",
        "claim_class",
        "owner",
        "supporting_evidence",
        "contradicting_evidence",
        "linked_patterns",
        "first_seen",
        "last_reviewed",
        "review_notes",
    },
    "skeptical_review": {
        "reviewed_record_id",
        "reviewed_record_type",
        "approved",
        "approved_for_dreamer",
        "risk",
        "recommended_action",
        "issues",
        "priority_questions",
        "alternative_hypotheses",
        "evidence_needed",
        "claim_updates",
        "confidence_adjustments",
        "reasoning_errors",
        "pattern_status",
        "counterexample_search",
    },
    "pattern": {
        "pattern_type",
        "hypothesis",
        "supporting_records",
        "counterexamples",
        "alternative_explanations",
        "confidence",
        "status",
        "first_seen",
        "last_reviewed",
        "predictions",
        "review_notes",
        "counterexample_search",
        "strength_override",
        "integration_override",
    },
    "prediction": {
        "expectation",
        "trigger",
        "source_id",
        "verdict",
        "verdict_evidence",
        "verdict_note",
        "scored_at",
        "score_attempts",
    },
    "evidence_correction": {"corrects", "date", "field_corrected", "original_value", "corrected_value", "basis", "approved_by"},
    "state": {"ttl_days", "sources", "confidence", "confidence_basis", "last_confirmed"},
    "decision": {"revisit_after", "revisit_conditions", "alternatives_considered"},
    "open_loop": {"priority", "owner", "next_action", "blocked_by", "review_after"},
    "report": set(),
    "contradiction_log": set(),
    "self_episode": {"event_kind", "source_refs", "outcome"},
    "self_belief": {"belief_confidence", "evidence_refs", "revisions"},
}

ENUMS = {
    "type": {
        "entity",
        "episode",
        "knowledge",
        "artifact",
        "evidence",
        "claim",
        "pattern",
        "skeptical_review",
        "evidence_correction",
        "state",
        "decision",
        "open_loop",
        "report",
        "contradiction_log",
        "self_episode",
        "self_belief",
    },
    "status": {
        "active",
        "archived",
        "stale",
        "resolved",
        "disputed",
        "stale_unresolved",
        "confirmed",
        "rejected",
        "superseded",
        "candidate",
        "active_hypothesis",
        "skeptic_reviewed",
        "supported",
        "integrated",
        "retired",
        "discovered",
        "parsed",
        "evidence_extracted",
        "failed",
        "skipped",
        "quarantined",
    },
    "significance": {"high", "medium", "low"},
    "domain_primary": {
        "physical",
        "environmental",
        "financial",
        "relational",
        "work",
        "status",
        "appearance",
        "competence",
        "social_presence",
        "desirability",
        "cross_arena",
    },
    "privacy": {
        "personal",
        "personal_sensitive",
        "family",
        "legal",
        "work",
        "financial",
        "health",
        "children",
        "business",
        "sealed",
    },
    "disclosure": {"private", "personal", "public"},
    "confidence": {"high", "medium", "low"},
    "source": {"elicitor", "extraction", "manual"},
    "priority": {"high", "medium", "low"},
    "source_type": {
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
        # Ship 1 of WO-PSYCHE writes owner check-ins with this source_type;
        # it was missing here, so every real check-in failed validation
        # (latent — found while building Ship 2's test fixtures).
        "checkin",
    },
    "sensitivity": {"low", "medium", "high", "restricted", "sealed"},
    "reliability": {"low", "medium", "high"},
    "claim_class": {
        "observation",
        "inference",
        "interpretation",
        "prediction",
        "motive_hypothesis",
        "value_statement",
        "identity_claim",
        "psychological_hypothesis",
        "self_report",
    },
    "claim_owner": {"user", "agent", "external_actor"},
    "pattern_type": {
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
        "support_strategy",
        "other",
    },
    "subtype": {"person", "place", "thing", "project", "organization", "text_message", "photo", "document", "call_log", "receipt", "legal", "screenshot"},
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


def validate_vault(vault: Path | None = None) -> ValidationReport:
    vault = vault or vault_root()
    report = ValidationReport()
    schemas = load_schemas()
    seen_ids: dict[str, Path] = {}
    manifests_root = vault / "manifests"
    manifest_core = manifests_root / "manifest-core.md"
    manifest_count = None
    if manifest_core.exists():
        manifest_count = sum(1 for line in manifest_core.read_text(encoding="utf-8").splitlines() if line.startswith("- "))
        if manifest_count > 200:
            report.add(manifest_core, f"manifest-core.md exceeds 200 entries ({manifest_count})")

    for path in iter_markdown_files(vault):
        if not _is_structured_record(path, vault):
            continue
        try:
            doc = load_markdown(path)
        except FrontmatterError as exc:
            report.add(path, str(exc))
            continue

        frontmatter = normalize_domain_fields(doc.frontmatter)
        file_type = frontmatter.get("type")
        if not file_type:
            report.add(path, "Missing required frontmatter field: type")
            continue
        if file_type not in TYPE_FIELDS:
            report.add(path, f"Unsupported type: {file_type}")
            continue

        _validate_universal(path, frontmatter, report)
        _validate_type_specific(path, frontmatter, report)
        _validate_schema(path, frontmatter, schemas, report)
        _validate_frontmatter_consistency(path, doc.body, frontmatter, report)
        file_id = str(frontmatter.get("id", ""))
        if file_id:
            # Archived snapshots (vault/archive/) legitimately share ids with
            # each other and with their live record — epoch archiving and
            # entity merges both preserve the original id. The duplicate-id
            # audit is about live records colliding.
            try:
                in_archive = "archive" in path.relative_to(vault).parts
            except ValueError:
                in_archive = "archive" in path.parts
            if in_archive:
                pass
            elif (previous := seen_ids.get(file_id)) is not None:
                report.add(path, f"Duplicate id {file_id} also used in {previous}")
            else:
                seen_ids[file_id] = path

    _validate_links(vault, seen_ids, report)
    _validate_episode_sources(vault, report)
    _validate_state_staleness(vault, report)
    _validate_wikilinks(vault, seen_ids, report)
    _validate_alias_uniqueness(vault, report)
    return report


def _validate_universal(path: Path, frontmatter: dict[str, Any], report: ValidationReport) -> None:
    for field in UNIVERSAL_REQUIRED:
        if field not in frontmatter:
            report.add(path, f"Missing required frontmatter field: {field}")
    for field in ["created", "updated", "last_confirmed", "review_after"]:
        if field in frontmatter and frontmatter[field]:
            try:
                date.fromisoformat(str(frontmatter[field]))
            except ValueError:
                report.add(path, f"Invalid ISO date in {field}: {frontmatter[field]}")
    for field, allowed in ENUMS.items():
        value = frontmatter.get(field)
        if value is None:
            continue
        if field == "confidence" and str(frontmatter.get("type")) in {"claim", "pattern"}:
            continue
        if field in {"domain_primary", "privacy", "status", "significance", "confidence", "source", "priority", "source_type", "sensitivity", "reliability"} and str(value) not in allowed:
            report.add(path, f"Invalid {field}: {value}")
    for field in ["domain_secondary", "arena_secondary", "compartments", "allowed_contexts", "blocked_contexts", "links"]:
        if field in frontmatter and not isinstance(frontmatter[field], list):
            report.add(path, f"{field} must be a list")
    disclosure = frontmatter.get("disclosure")
    if disclosure is not None and str(disclosure) not in ENUMS["disclosure"]:
        report.add(path, f"Invalid disclosure: {disclosure}")


def _validate_type_specific(path: Path, frontmatter: dict[str, Any], report: ValidationReport) -> None:
    file_type = str(frontmatter.get("type"))
    missing = TYPE_FIELDS[file_type] - frontmatter.keys()
    for field in sorted(missing):
        report.add(path, f"Missing required {file_type} field: {field}")
    if file_type == "claim":
        confidence = frontmatter.get("confidence")
        if not isinstance(confidence, (int, float)):
            report.add(path, "claim confidence must be numeric")
        elif not 0.0 <= float(confidence) <= 1.0:
            report.add(path, "claim confidence must be between 0.0 and 1.0")
        if str(frontmatter.get("claim_class", "")) not in ENUMS["claim_class"]:
            report.add(path, f"Invalid claim_class: {frontmatter.get('claim_class')}")
        if str(frontmatter.get("owner", "")) not in ENUMS["claim_owner"]:
            report.add(path, f"Invalid owner: {frontmatter.get('owner')}")
        # WO-GROUND Seam B gate: a self-report above medium confidence is the
        # poison-record shape (2026-07-06) and must not validate.
        if (
            str(frontmatter.get("claim_class", "")) == "self_report"
            and isinstance(confidence, (int, float))
            and float(confidence) > 0.6
        ):
            report.add(path, "self_report claims are capped at medium confidence (0.6)")
    elif file_type == "prediction":
        # WO-PSYCHE Ship 2 gates: attribution is mandatory; a scored record
        # carries its verdict; a hit/miss without cited evidence is a vibe.
        if str(frontmatter.get("status", "")) not in {"pending", "scored", "retired"}:
            report.add(path, f"Invalid prediction status: {frontmatter.get('status')}")
        verdict = str(frontmatter.get("verdict", ""))
        if verdict not in {"", "hit", "miss", "unclear"}:
            report.add(path, f"Invalid prediction verdict: {verdict}")
        if not str(frontmatter.get("source_id", "")).strip():
            report.add(path, "prediction requires a source_id (framework or pattern)")
        if str(frontmatter.get("status")) == "scored" and verdict not in {"hit", "miss", "unclear"}:
            report.add(path, "a scored prediction must carry a verdict")
        if verdict in {"hit", "miss"} and not (frontmatter.get("verdict_evidence") or []):
            report.add(path, "a hit/miss verdict requires verdict_evidence")
        if _prediction_has_banned_language(frontmatter):
            report.add(path, "Prediction expectations must not use diagnostic or pathologizing language")
    elif file_type == "pattern":
        confidence = frontmatter.get("confidence")
        if not isinstance(confidence, (int, float)):
            report.add(path, "pattern confidence must be numeric")
        elif not 0.0 <= float(confidence) <= 1.0:
            report.add(path, "pattern confidence must be between 0.0 and 1.0")
        if str(frontmatter.get("pattern_type", "")) not in ENUMS["pattern_type"]:
            report.add(path, f"Invalid pattern_type: {frontmatter.get('pattern_type')}")
        if _pattern_has_banned_language(frontmatter):
            report.add(path, "Pattern hypotheses must not use diagnostic or pathologizing language")
        supporting_records = frontmatter.get("supporting_records") or []
        if not isinstance(supporting_records, list):
            report.add(path, "supporting_records must be a list")
        else:
            if float(confidence) >= 0.5 and len(supporting_records) < 2:
                report.add(path, "Patterns with medium/high confidence need at least two supporting_records")
            if float(confidence) < 0.5 and len(supporting_records) < 1:
                report.add(path, "Low-confidence patterns still need at least one supporting_record or explicit fallback evidence")
        if not (frontmatter.get("alternative_explanations") or []):
            report.add(path, "pattern requires at least one alternative_explanation")
        counterexample_search = frontmatter.get("counterexample_search") or {}
        if not isinstance(counterexample_search, dict):
            report.add(path, "counterexample_search must be an object")
        else:
            if not bool(counterexample_search.get("performed", False)):
                report.add(path, "pattern requires a performed counterexample search")
            if not (counterexample_search.get("counterexamples") or frontmatter.get("counterexamples") or []):
                report.add(path, "pattern requires a counterexample search result")
        if "strength_override" not in frontmatter:
            report.add(path, "pattern requires an explicit strength_override field")
        integration_override = frontmatter.get("integration_override")
        if not isinstance(integration_override, dict):
            report.add(path, "integration_override must be an object")
        else:
            if "enabled" not in integration_override or "reason" not in integration_override or "approved_by" not in integration_override:
                report.add(path, "integration_override requires enabled, reason, and approved_by")
        if not (frontmatter.get("evidence_needed") or []):
            report.add(path, "pattern requires evidence_needed guidance")
    elif file_type == "evidence":
        if str(frontmatter.get("source_type", "")) not in ENUMS["source_type"]:
            report.add(path, f"Invalid source_type: {frontmatter.get('source_type')}")
        if str(frontmatter.get("sensitivity", "")) not in ENUMS["sensitivity"]:
            report.add(path, f"Invalid sensitivity: {frontmatter.get('sensitivity')}")
        if str(frontmatter.get("reliability", "")) not in ENUMS["reliability"]:
            report.add(path, f"Invalid reliability: {frontmatter.get('reliability')}")
    elif file_type == "artifact":
        if str(frontmatter.get("source_type", "")) not in {"file", "markdown", "text", "pdf", "image", "email_export", "sms_export", "other"}:
            report.add(path, f"Invalid source_type: {frontmatter.get('source_type')}")
        if not str(frontmatter.get("source_path", "")).strip():
            report.add(path, "Missing source_path")
        if not str(frontmatter.get("artifact_hash", "")).strip():
            report.add(path, "Missing artifact_hash")
        if not str(frontmatter.get("file_name", "")).strip():
            report.add(path, "Missing file_name")
        if not str(frontmatter.get("file_ext", "")).strip():
            report.add(path, "Missing file_ext")
        try:
            size_bytes = int(frontmatter.get("size_bytes"))
            if size_bytes < 0:
                raise ValueError
        except (TypeError, ValueError):
            report.add(path, f"Invalid size_bytes: {frontmatter.get('size_bytes')}")
        if str(frontmatter.get("ingestion_status", "")) not in {"discovered", "parsed", "evidence_extracted", "failed", "skipped", "quarantined"}:
            report.add(path, f"Invalid ingestion_status: {frontmatter.get('ingestion_status')}")
        if str(frontmatter.get("sensitivity", "")) not in ENUMS["sensitivity"]:
            report.add(path, f"Invalid sensitivity: {frontmatter.get('sensitivity')}")
    elif file_type == "skeptical_review":
        if str(frontmatter.get("risk", "")) not in {"low", "medium", "high"}:
            report.add(path, f"Invalid risk: {frontmatter.get('risk')}")
        if str(frontmatter.get("recommended_action", "")) not in {"approve", "revise", "hold"}:
            report.add(path, f"Invalid recommended_action: {frontmatter.get('recommended_action')}")
        if "approved_for_dreamer" in frontmatter and not isinstance(frontmatter.get("approved_for_dreamer"), bool):
            report.add(path, "approved_for_dreamer must be boolean")
        if "counterexample_search" in frontmatter and not isinstance(frontmatter.get("counterexample_search"), dict):
            report.add(path, "counterexample_search must be an object")


def _pattern_has_banned_language(frontmatter: dict[str, Any]) -> bool:
    text = " ".join(
        str(frontmatter.get(field, ""))
        for field in ["hypothesis", "summary", "review_notes"]
    ).lower()
    return any(term in text for term in BANNED_PATTERN_TERMS)


def _prediction_has_banned_language(frontmatter: dict[str, Any]) -> bool:
    """Same clinical-label rule as patterns (WO-PSYCHE §1 rule 2): the ledger
    inherits the hypothesis layer's language discipline."""
    text = " ".join(
        str(frontmatter.get(field, ""))
        for field in ["expectation", "trigger", "summary", "verdict_note"]
    ).lower()
    return any(term in text for term in BANNED_PATTERN_TERMS)


def _validate_schema(path: Path, frontmatter: dict[str, Any], schemas: dict[str, dict[str, Any]], report: ValidationReport) -> None:
    schema = schemas.get(str(frontmatter.get("type")))
    if not schema:
        return
    _validate_against_schema(path, frontmatter, schema, report)


def _validate_against_schema(path: Path, data: Any, schema: dict[str, Any], report: ValidationReport, prefix: str = "") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(data, dict):
            report.add(path, f"{prefix or 'frontmatter'} must be an object")
            return
        required = schema.get("required", [])
        for field in required:
            if field not in data:
                report.add(path, f"Schema required field missing: {prefix}{field}")
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                _validate_against_schema(path, value, properties[key], report, prefix=f"{prefix}{key}.")
    elif expected_type == "array":
        if not isinstance(data, list):
            report.add(path, f"{prefix[:-1] or 'value'} must be an array")
            return
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(data):
                _validate_against_schema(path, item, item_schema, report, prefix=f"{prefix}{index}.")
    elif expected_type == "string":
        if not isinstance(data, str):
            report.add(path, f"{prefix[:-1] or 'value'} must be a string")
            return
        enum = schema.get("enum")
        if enum and data not in enum:
            report.add(path, f"{prefix[:-1] or 'value'} must be one of {enum}")
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, data):
            report.add(path, f"{prefix[:-1] or 'value'} does not match schema pattern")
    elif expected_type == "integer":
        if not isinstance(data, int):
            report.add(path, f"{prefix[:-1] or 'value'} must be an integer")
    elif expected_type == "boolean":
        if not isinstance(data, bool):
            report.add(path, f"{prefix[:-1] or 'value'} must be a boolean")


def _validate_frontmatter_consistency(path: Path, body: str, frontmatter: dict[str, Any], report: ValidationReport) -> None:
    if str(frontmatter.get("type")) == "episode":
        headings = {line.strip() for line in body.splitlines() if line.startswith("## ")}
        required = {
            "## Event Timeline",
            "## Documented Evidence",
            "## User-Reported Context",
            "## Interpretations",
            "## Operational Consequences",
            "## Open Questions",
        }
        missing = required - headings
        if missing and str(frontmatter.get("source")) != "elicitor":
            for heading in missing:
                report.add(path, f"Missing episode section header: {heading}")
        if frontmatter.get("significance") == "high" and "## Claims" not in body:
            report.add(path, "High-significance episodes should include a Claims section")
        if frontmatter.get("significance") == "high":
            links = [str(link) for link in frontmatter.get("links", []) if isinstance(link, str)]
            has_operational_link = any(
                link.startswith("decision.") or link.startswith("open_loop.") or link.startswith("state.")
                for link in links
            )
            if not has_operational_link and not str(frontmatter.get("significance_rationale", "")).strip():
                report.add(
                    path,
                    "High-significance episode needs a decision/open_loop/state link or significance_rationale",
                )


def _validate_links(vault: Path, seen_ids: dict[str, Path], report: ValidationReport) -> None:
    for path in iter_markdown_files(vault):
        if path.name in {"identity.md", "operating-style.md", "current-brief.md"}:
            continue
        try:
            doc = load_markdown(path)
        except FrontmatterError:
            continue
        links = doc.frontmatter.get("links", [])
        if not isinstance(links, list):
            continue
        for link in links:
            if isinstance(link, str):
                if link in seen_ids:
                    continue
                if not (vault / link).exists():
                    report.add(path, f"Link target does not exist: {link}")


def _validate_episode_sources(vault: Path, report: ValidationReport) -> None:
    for path in (vault / "episodes").glob("*.md"):
        try:
            doc = load_markdown(path)
        except FrontmatterError as exc:
            report.add(path, str(exc))
            continue
        if str(doc.frontmatter.get("type")) != "episode":
            continue
        if "source" not in doc.frontmatter:
            report.add(path, "Episode missing source field")


def _validate_state_staleness(vault: Path, report: ValidationReport) -> None:
    today = date.today()
    for path in (vault / "state").glob("*.md"):
        try:
            doc = load_markdown(path)
        except FrontmatterError:
            continue
        fm = doc.frontmatter
        ttl = fm.get("ttl_days")
        updated = fm.get("updated")
        if ttl and updated:
            try:
                age = (today - date.fromisoformat(str(updated))).days
                if age > int(ttl):
                    report.add(path, f"State file is stale: age={age} days, ttl={ttl} days", severity="warning")
            except (ValueError, TypeError):
                pass
        review_after = fm.get("review_after")
        if review_after:
            try:
                if today > date.fromisoformat(str(review_after)):
                    report.add(path, f"State file is past review_after date: {review_after}", severity="warning")
            except ValueError:
                pass


def _validate_wikilinks(vault: Path, seen_ids: dict[str, Path], report: ValidationReport) -> None:
    _WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
    for path in iter_markdown_files(vault):
        if not _is_structured_record(path, vault):
            continue
        try:
            doc = load_markdown(path)
        except FrontmatterError:
            continue
        for match in _WIKILINK_RE.finditer(doc.body):
            target = match.group(1).strip()
            if not target:
                continue
            target_clean = target.split("|")[0].strip()
            if target_clean in seen_ids:
                continue
            if (vault / target_clean).exists():
                continue
            report.add(path, f"Wikilink target not found: [[{target_clean}]]", severity="warning")


def _validate_alias_uniqueness(vault: Path, report: ValidationReport) -> None:
    """Warn when the same alias resolves to multiple entities (spec §7.8)."""
    import sqlite3
    from ..paths import sqlite_path
    db = sqlite_path()
    if not db.exists():
        return
    try:
        conn = _db_connect(db)
        try:
            rows = conn.execute(
                """
                SELECT alias, COUNT(DISTINCT entity_id) AS cnt, GROUP_CONCAT(entity_id, ', ') AS ids
                FROM entity_aliases
                GROUP BY alias
                HAVING cnt > 1
                """
            ).fetchall()
            for row in rows:
                alias, count, ids = row[0], row[1], row[2]
                report.add(
                    vault / "entities",
                    f"Alias '{alias}' resolves to {count} entities: {ids}",
                    severity="warning",
                )
        finally:
            conn.close()
    except Exception as exc:
        report.add(
            vault / "entities",
            f"Alias ambiguity audit skipped — index unreadable: {exc}",
            severity="warning",
        )


def _is_structured_record(path: Path, vault: Path) -> bool:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return False
    if not rel.parts:
        return False
    if rel.parts[0] in {"primer", "transcripts", "manifests"}:
        return False
    if rel.name == "backup.md":
        return False
    return rel.parts[0] in {"entities", "episodes", "knowledge", "evidence", "state", "decisions", "open_loops", "claims", "patterns", "reviews", "reports", "contradictions", "archive", "self"}


def format_report(report: ValidationReport) -> str:
    if not report.issues:
        return "Validation passed."
    lines = [f"Validation failed: {report.summary()}"]
    for issue in report.issues:
        lines.append(f"- {issue.severity.upper()}: {issue.path}: {issue.message}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    report = validate_vault()
    print(format_report(report))
    return 0 if report.ok else 1
