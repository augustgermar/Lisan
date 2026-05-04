from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import FrontmatterError, load_markdown
from ..paths import vault_root, schemas_dir, repo_root
from ..schemas import load_schemas
from ..tools.common import iter_markdown_files, parse_date


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
    "arena_primary",
    "arena_secondary",
    "privacy",
    "compartments",
    "allowed_contexts",
    "blocked_contexts",
    "summary",
    "links",
    "confidence",
    "confidence_basis",
    "last_confirmed",
    "review_after",
}

TYPE_FIELDS = {
    "entity": {"subtype", "canonical_name", "aliases", "disambiguation", "epoch", "epoch_started", "previous_epochs"},
    "episode": {"entities", "evidence", "claims", "source"},
    "knowledge": set(),
    "evidence": {"subtype", "date_of_artifact", "supports", "corrections"},
    "state": {"ttl_days", "sources", "confidence", "confidence_basis", "last_confirmed"},
    "decision": {"revisit_after", "revisit_conditions", "alternatives_considered"},
    "open_loop": {"priority", "owner", "next_action", "blocked_by", "review_after"},
}

ENUMS = {
    "type": {"entity", "episode", "knowledge", "evidence", "state", "decision", "open_loop"},
    "status": {"active", "archived", "stale", "resolved", "disputed", "stale_unresolved"},
    "significance": {"high", "medium", "low"},
    "arena_primary": {
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
    },
    "confidence": {"high", "medium", "low"},
    "source": {"elicitor", "extraction", "manual"},
    "priority": {"high", "medium", "low"},
    "subtype": {"person", "place", "project", "organization", "text_message", "photo", "document", "call_log", "receipt", "legal", "screenshot"},
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

        frontmatter = doc.frontmatter
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
            previous = seen_ids.get(file_id)
            if previous is not None:
                report.add(path, f"Duplicate id {file_id} also used in {previous}")
            else:
                seen_ids[file_id] = path

    _validate_links(vault, seen_ids, report)
    _validate_episode_sources(vault, report)
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
        if field in {"arena_primary", "privacy", "status", "significance", "confidence", "source", "priority"} and str(value) not in allowed:
            report.add(path, f"Invalid {field}: {value}")
    for field in ["arena_secondary", "compartments", "allowed_contexts", "blocked_contexts", "links"]:
        if field in frontmatter and not isinstance(frontmatter[field], list):
            report.add(path, f"{field} must be a list")


def _validate_type_specific(path: Path, frontmatter: dict[str, Any], report: ValidationReport) -> None:
    file_type = str(frontmatter.get("type"))
    missing = TYPE_FIELDS[file_type] - frontmatter.keys()
    for field in sorted(missing):
        report.add(path, f"Missing required {file_type} field: {field}")


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
        if missing:
            source = str(frontmatter.get("source"))
            if source == "elicitor":
                for heading in missing:
                    report.add(path, f"Missing episode section header: {heading}", severity="error")
            else:
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


def _is_structured_record(path: Path, vault: Path) -> bool:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return False
    if not rel.parts:
        return False
    if rel.parts[0] in {"primer", "arenas", "transcripts", "manifests", "reports"}:
        return False
    if rel.name == "backup.md":
        return False
    return rel.parts[0] in {"entities", "episodes", "knowledge", "evidence", "state", "decisions", "open_loops", "archive"}


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
