from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import vault_root
from ..utils import today_iso
from .analyst_ops import AnalystRunResult, run_analyst_scan
from .dreamer_ops import audit_patterns
from .manifest_gen import generate_manifests
from .current_brief import write_current_brief
from .rebuild_index import rebuild_index
from .validator import ValidationReport, validate_vault


@dataclass(slots=True)
class SeedPatternRow:
    pattern_id: str
    hypothesis: str
    pattern_type: str
    supporting_records: list[str]
    counterexamples: list[str]
    confidence: float
    skeptic_approved: bool
    approved_for_dreamer: bool
    dreamer_eligible: bool
    blocked_reasons: list[str]
    review_summary: str
    review_path: str | None


@dataclass(slots=True)
class SeedEvaluationResult:
    vault: Path
    report_path: Path
    report_text: str
    validation_before: ValidationReport
    validation_after: ValidationReport
    index_before: dict[str, int]
    index_after: dict[str, int]
    analyst: AnalystRunResult
    audit: dict[str, Any]
    patterns: list[SeedPatternRow]


def run_seed_evaluation(
    vault: Path | None = None,
    db_path: Path | None = None,
    embeddings_file: Path | None = None,
) -> SeedEvaluationResult:
    vault = vault or vault_root()
    vault.mkdir(parents=True, exist_ok=True)
    validation_before, index_before = _sync_fixture_vault(vault, db_path=db_path, embeddings_file=embeddings_file)

    analyst = run_analyst_scan(vault=vault)

    validation_after, index_after = _sync_fixture_vault(vault, db_path=db_path, embeddings_file=embeddings_file)
    audit = audit_patterns(vault)
    patterns = _collect_pattern_rows(vault, audit)
    report_text = _render_report(vault, validation_before, validation_after, index_before, index_after, analyst, audit, patterns)
    report_path = _write_report(vault, report_text, analyst, patterns)
    return SeedEvaluationResult(
        vault=vault,
        report_path=report_path,
        report_text=report_text,
        validation_before=validation_before,
        validation_after=validation_after,
        index_before=index_before,
        index_after=index_after,
        analyst=analyst,
        audit=audit,
        patterns=patterns,
    )


def _sync_fixture_vault(
    vault: Path,
    db_path: Path | None = None,
    embeddings_file: Path | None = None,
) -> tuple[ValidationReport, dict[str, int]]:
    generate_manifests(vault, write=True)
    write_current_brief(vault)
    validation = validate_vault(vault)
    counts = rebuild_index(vault, db_path=db_path, embeddings_file=embeddings_file)
    return validation, counts


def _collect_pattern_rows(vault: Path, audit: dict[str, Any]) -> list[SeedPatternRow]:
    eligible_ids = {str(entry.get("id") or "") for entry in audit.get("eligible", [])}
    blocked_map = {str(entry.get("id") or ""): list(entry.get("reasons") or []) for entry in audit.get("blocked", [])}
    reviews = _load_pattern_reviews(vault)
    review_map = {str(review.get("reviewed_record_id") or ""): review for review in reviews}
    rows: list[SeedPatternRow] = []
    for path in sorted((vault / "patterns").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = doc.frontmatter
        pattern_id = str(fm.get("id") or path.stem)
        review = review_map.get(pattern_id, {})
        review_path = _review_path_for_pattern(vault, pattern_id)
        rows.append(
            SeedPatternRow(
                pattern_id=pattern_id,
                hypothesis=str(fm.get("hypothesis") or fm.get("summary") or ""),
                pattern_type=str(fm.get("pattern_type") or "other"),
                supporting_records=[str(item) for item in list(fm.get("supporting_records") or [])],
                counterexamples=[str(item) for item in list(fm.get("counterexamples") or [])],
                confidence=float(fm.get("confidence") or 0.0),
                skeptic_approved=bool(review.get("approved", False)),
                approved_for_dreamer=bool(review.get("approved_for_dreamer", False)),
                dreamer_eligible=pattern_id in eligible_ids,
                blocked_reasons=blocked_map.get(pattern_id, []),
                review_summary=str(review.get("summary") or ""),
                review_path=str(review_path.relative_to(vault)) if review_path else None,
            )
        )
    return rows


def _load_pattern_reviews(vault: Path) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for path in sorted((vault / "reviews").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("reviewed_record_type")) != "pattern":
            continue
        reviews.append(doc.frontmatter)
    return reviews


def _review_path_for_pattern(vault: Path, pattern_id: str) -> Path | None:
    for path in sorted((vault / "reviews").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("reviewed_record_type")) == "pattern" and str(doc.frontmatter.get("reviewed_record_id") or "") == pattern_id:
            return path
    return None


def _render_report(
    vault: Path,
    validation_before: ValidationReport,
    validation_after: ValidationReport,
    index_before: dict[str, int],
    index_after: dict[str, int],
    analyst: AnalystRunResult,
    audit: dict[str, Any],
    patterns: list[SeedPatternRow],
) -> str:
    lines: list[str] = [
        "# Seed Evaluation Report",
        "",
        f"Vault: `{vault}`",
        f"Generated: {today_iso()}",
        "",
        "## Sync",
        f"- validation_before: {validation_before.summary()}",
        f"- validation_after: {validation_after.summary()}",
        f"- index_before: {index_before}",
        f"- index_after: {index_after}",
        f"- analyst_report: `{analyst.report_path}`",
        "",
        "## Pattern Summary",
    ]
    if not patterns:
        lines.append("- No patterns were generated.")
    for row in patterns:
        blocked = "; ".join(row.blocked_reasons) or "none"
        lines.extend(
            [
                f"### {row.pattern_id}",
                f"- type: {row.pattern_type}",
                f"- hypothesis: {row.hypothesis}",
                f"- supporting_records: {', '.join(row.supporting_records) or 'none'}",
                f"- counterexamples: {', '.join(row.counterexamples) or 'none'}",
                f"- confidence: {row.confidence:.2f}",
                f"- skeptic_approved: {str(row.skeptic_approved).lower()}",
                f"- approved_for_dreamer: {str(row.approved_for_dreamer).lower()}",
                f"- dreamer_eligible: {str(row.dreamer_eligible).lower()}",
                f"- blocked_reason: {blocked}",
                f"- review_summary: {row.review_summary or 'none'}",
                "",
            ]
        )

    lines.extend(
        [
            "## Dreamer Audit",
            f"- eligible_count: {len(audit.get('eligible', []))}",
            f"- blocked_count: {len(audit.get('blocked', []))}",
            f"- stale_count: {len(audit.get('stale', []))}",
            f"- disputed_count: {len(audit.get('disputed', []))}",
            f"- missing_counterexample_search_count: {len(audit.get('missing_counterexample_search', []))}",
            f"- low_evidence_count: {len(audit.get('low_evidence_count', []))}",
            f"- high_confidence_unresolved_count: {len(audit.get('high_confidence_unresolved', []))}",
            "",
            "## What To Watch",
            "- Hostile motive claims should remain disputed unless external evidence supports intent.",
            "- Overgeneralized identity claims should be downgraded when counterexamples exist.",
            "- Diagnostic language should be rejected or quarantined.",
            "- Patterns should not enter Dreamer until governance checks pass.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _write_report(vault: Path, report_text: str, analyst: AnalystRunResult, patterns: list[SeedPatternRow]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    path = vault / "reports" / f"seed-evaluation-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    links = [row.review_path for row in patterns if row.review_path]
    links.extend(str(p.relative_to(vault)) for p in analyst.pattern_paths + analyst.review_paths)
    write_markdown(
        path,
        {
            "id": f"report.seed_evaluation.{stamp}",
            "type": "report",
            "created": today_iso(),
            "updated": today_iso(),
            "status": "active",
            "significance": "medium",
            "domain_primary": "cross_arena",
            "domain_secondary": [],
            "privacy": "personal",
            "compartments": [],
            "allowed_contexts": ["all"],
            "blocked_contexts": [],
            "summary": "Seeded self-model evaluation report",
            "links": links,
            "confidence": "low",
            "confidence_basis": "Seed evaluation run",
            "last_confirmed": today_iso(),
            "review_after": today_iso(),
            "task": "seed_evaluation",
        },
        report_text,
    )
    return path
