from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..agents import DreamerAgent
from ..frontmatter import load_markdown
from ..frontmatter import write_markdown
from ..paths import vault_root
from ..utils import today_iso
from .deixis import render_for_display
from .epistemic import canonical_pattern_status, pattern_age_days, pattern_minimum_age_days
from .primer_audit import build_primer_audit_bundle


def run_dreamer_task(
    vault: Path | None = None,
    task: str = "compress",
    provider: str | None = None,
    model: str | None = None,
) -> Path:
    vault = vault or vault_root()
    prompt_file = _prompt_for_task(task)
    bundle = _bundle_for_task(vault, task)
    agent = DreamerAgent(vault=vault, prompt_file=prompt_file)
    response = agent.run_json(bundle, significance="high", provider=provider, model=model, task=task)
    artifact_path = _apply_task_side_effect(vault, task, bundle, response)
    out = _output_path(vault, task)
    out.parent.mkdir(parents=True, exist_ok=True)
    _render_report(out, task, bundle, response, artifact_path)
    return out


def _prompt_for_task(task: str) -> str:
    mapping = {
        "compress": "dreamer_compress_v1",
        "primer": "dreamer_primer_v1",
        "contradict": "dreamer_contradict_v1",
        "epoch": "dreamer_epoch_v1",
        "confidence": "dreamer_confidence_v1",
        "overfitting": "dreamer_overfitting_v1",
        "identity_anchor": "dreamer_identity_anchor_v1",
    }
    return mapping.get(task, "dreamer_compress_v1")


def _bundle_for_task(vault: Path, task: str) -> str:
    if task == "primer":
        return build_primer_audit_bundle(vault) + _bundle_approved_patterns(vault)
    if task == "contradict":
        return _bundle_recent_episodes(vault, days=120, include_states=True, include_entities=False) + _bundle_approved_patterns(vault)
    if task == "confidence":
        return _bundle_confidence(vault) + _bundle_approved_patterns(vault)
    if task == "epoch":
        return _bundle_entities(vault) + _bundle_approved_patterns(vault)
    if task == "overfitting":
        return _bundle_overfitting(vault) + _bundle_approved_patterns(vault)
    if task == "identity_anchor":
        return _bundle_identity(vault) + _bundle_approved_patterns(vault)
    return _bundle_recent_episodes(vault, days=365, include_states=True, include_entities=True) + _bundle_approved_patterns(vault)


def _bundle_recent_episodes(vault: Path, days: int, include_states: bool, include_entities: bool) -> str:
    lines: list[str] = []
    cutoff = date.today() - timedelta(days=days)
    lines.append("## Recent Episodes")
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        created = doc.frontmatter.get("created")
        if not created:
            continue
        try:
            if date.fromisoformat(str(created)) < cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")

    if include_states:
        lines.append("## State Files")
        for path in sorted((vault / "state").glob("*.md")):
            try:
                load_markdown(path)
            except Exception:
                continue
            lines.append(f"### {path.name}")
            lines.append(path.read_text(encoding="utf-8").strip())
            lines.append("")

    if include_entities:
        lines.append("## Entities")
        for path in sorted((vault / "entities").rglob("*.md")):
            try:
                load_markdown(path)
            except Exception:
                continue
            lines.append(f"### {path.relative_to(vault)}")
            lines.append(path.read_text(encoding="utf-8").strip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_confidence(vault: Path) -> str:
    lines = ["## Confidence Candidates", ""]
    cutoff = date.today() - timedelta(days=365)
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("significance")) != "high":
            continue
        created = doc.frontmatter.get("created")
        try:
            if not created or date.fromisoformat(str(created)) >= cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_entities(vault: Path) -> str:
    lines = ["## Entity Candidates", ""]
    for path in sorted((vault / "entities").rglob("*.md")):
        try:
            load_markdown(path)
        except Exception:
            continue
        lines.append(f"### {path.relative_to(vault)}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_overfitting(vault: Path) -> str:
    lines = ["## Overfitting Candidates", ""]
    cutoff = date.today() - timedelta(days=365)
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("significance")) != "high":
            continue
        created = doc.frontmatter.get("created")
        try:
            if not created or date.fromisoformat(str(created)) >= cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_identity(vault: Path) -> str:
    lines = ["## Identity Anchors", ""]
    lines.append(_bundle_recent_episodes(vault, days=180, include_states=True, include_entities=True))
    return "\n".join(lines).rstrip() + "\n"


def _bundle_approved_patterns(vault: Path) -> str:
    audit = audit_patterns(vault)
    lines = ["## Approved Pattern Hypotheses", ""]
    found = False
    for entry in audit["eligible"]:
        path = Path(entry["path"])
        if not path.exists():
            continue
        found = True
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    if not found:
        lines.append("- None")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def audit_patterns(vault: Path) -> dict[str, Any]:
    patterns_root = vault / "patterns"
    reviews_root = vault / "reviews"
    review_map: dict[str, dict[str, Any]] = {}
    all_records: dict[str, dict[str, Any]] = {}

    for path in sorted(vault.rglob("*.md")):
        if path.parts[-2] in {"manifests", "transcripts", "drafts"}:
            continue
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = dict(doc.frontmatter)
        record_id = str(fm.get("id") or "")
        if record_id:
            all_records[record_id] = fm
        if path.is_relative_to(reviews_root):
            if str(fm.get("reviewed_record_type")) == "pattern" and str(fm.get("reviewed_record_id") or ""):
                review_map[str(fm.get("reviewed_record_id"))] = fm

    totals: dict[str, int] = {}
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    disputed: list[dict[str, Any]] = []
    missing_counterexample_search: list[dict[str, Any]] = []
    low_evidence_count: list[dict[str, Any]] = []
    high_confidence_unresolved: list[dict[str, Any]] = []

    for path in sorted(patterns_root.glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = dict(doc.frontmatter)
        pattern_id = str(fm.get("id") or path.stem)
        status = canonical_pattern_status(str(fm.get("status") or "candidate"))
        totals[status] = totals.get(status, 0) + 1
        review = review_map.get(pattern_id, {})
        reasons = _pattern_blockers(fm, review, all_records)
        entry = {
            "id": pattern_id,
            "path": str(path),
            "status": status,
            "confidence": float(fm.get("confidence") or 0.0),
            "pattern_type": str(fm.get("pattern_type") or "other"),
            "support_count": len(list(fm.get("supporting_records") or [])),
            "reviewed": bool(review),
            "approved_for_dreamer": bool(review.get("approved_for_dreamer", False)),
            "reasons": reasons,
        }
        if status == "stale":
            stale.append(entry)
        if status == "disputed":
            disputed.append(entry)
        if not bool((fm.get("counterexample_search") or {}).get("performed", False)):
            missing_counterexample_search.append(entry)
        if entry["support_count"] < 3 and not bool(fm.get("strength_override", False)):
            low_evidence_count.append(entry)
        if entry["confidence"] >= 0.75 and _has_unresolved_high_severity_contradiction(fm, all_records):
            high_confidence_unresolved.append(entry)
        if not reasons:
            eligible.append(entry)
        else:
            blocked.append(entry)

    return {
        "totals": totals,
        "eligible": eligible,
        "blocked": blocked,
        "stale": stale,
        "disputed": disputed,
        "missing_counterexample_search": missing_counterexample_search,
        "low_evidence_count": low_evidence_count,
        "high_confidence_unresolved": high_confidence_unresolved,
    }


def format_pattern_audit(report: dict[str, Any]) -> str:
    lines = ["Pattern Audit", ""]
    lines.append("Total patterns by status:")
    totals = report.get("totals", {})
    if totals:
        for status in sorted(totals):
            lines.append(f"- {status}: {totals[status]}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append(f"Patterns eligible for Dreamer: {len(report.get('eligible', []))}")
    for entry in report.get("eligible", []):
        lines.append(f"- {entry['id']} ({entry['status']}, confidence {entry['confidence']:.2f})")
    lines.append("")
    lines.append("Patterns blocked from Dreamer and why:")
    if report.get("blocked"):
        for entry in report.get("blocked", []):
            reasons = "; ".join(entry.get("reasons") or [])
            lines.append(f"- {entry['id']}: {reasons}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append(f"Stale patterns: {len(report.get('stale', []))}")
    for entry in report.get("stale", []):
        lines.append(f"- {entry['id']}")
    lines.append("")
    lines.append(f"Disputed patterns: {len(report.get('disputed', []))}")
    for entry in report.get("disputed", []):
        lines.append(f"- {entry['id']}")
    lines.append("")
    lines.append(f"Patterns missing counterexample search: {len(report.get('missing_counterexample_search', []))}")
    for entry in report.get("missing_counterexample_search", []):
        lines.append(f"- {entry['id']}")
    lines.append("")
    lines.append(f"Patterns with low evidence count: {len(report.get('low_evidence_count', []))}")
    for entry in report.get("low_evidence_count", []):
        lines.append(f"- {entry['id']}")
    lines.append("")
    lines.append(f"High-confidence patterns with unresolved contradictions: {len(report.get('high_confidence_unresolved', []))}")
    for entry in report.get("high_confidence_unresolved", []):
        lines.append(f"- {entry['id']}")
    return "\n".join(lines).rstrip() + "\n"


def _pattern_blockers(pattern: dict[str, Any], review: dict[str, Any], records: dict[str, dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    status = canonical_pattern_status(str(pattern.get("status") or "candidate"))
    if status not in {"skeptic_reviewed", "supported"}:
        reasons.append(f"status={status}")
    if status in {"disputed", "stale", "rejected", "retired", "integrated"}:
        reasons.append(f"status_blocked={status}")

    if not bool(review.get("approved_for_dreamer", False)):
        reasons.append("skeptic_review.approved_for_dreamer=false")

    support_count = len(list(pattern.get("supporting_records") or []))
    if support_count < 3 and not bool(pattern.get("strength_override", False)):
        reasons.append(f"supporting_records<{3}")

    counterexample_search = pattern.get("counterexample_search") if isinstance(pattern.get("counterexample_search"), dict) else {}
    if not bool(counterexample_search.get("performed", False)):
        reasons.append("counterexample_search.performed=false")

    if len(list(pattern.get("alternative_explanations") or [])) < 1:
        reasons.append("missing_alternative_explanations")

    confidence = float(pattern.get("confidence") or 0.0)
    if confidence < 0.65:
        reasons.append(f"confidence<{0.65}")

    age_days = pattern_age_days(pattern)
    min_age = pattern_minimum_age_days(pattern)
    override = pattern.get("integration_override") if isinstance(pattern.get("integration_override"), dict) else {}
    override_enabled = bool(override.get("enabled", False))
    override_reason = str(override.get("reason") or "").strip()
    override_approved_by = str(override.get("approved_by") or "").strip()
    if age_days is None:
        reasons.append("missing_created_date")
    elif age_days < min_age and not (override_enabled and override_reason and override_approved_by == "user"):
        reasons.append(f"age<{min_age}d")
    elif age_days < min_age and not override_enabled:
        reasons.append(f"age<{min_age}d")

    if status in {"stale", "rejected", "retired", "disputed"}:
        reasons.append(f"blocked_status={status}")

    if _has_unresolved_high_severity_contradiction(pattern, records):
        reasons.append("unresolved_high_severity_contradiction")

    if isinstance(pattern.get("integration_override"), dict) and bool(pattern.get("integration_override", {}).get("enabled", False)):
        if not override_reason:
            reasons.append("integration_override.reason_missing")
        if override_approved_by != "user":
            reasons.append("integration_override.approved_by!=user")

    return list(dict.fromkeys(reasons))


def _has_unresolved_high_severity_contradiction(pattern: dict[str, Any], records: dict[str, dict[str, Any]]) -> bool:
    linked_ids = set()
    linked_ids.update(str(item) for item in list(pattern.get("links") or []))
    linked_ids.update(str(item) for item in list(pattern.get("supporting_records") or []))
    linked_ids.update(str(item) for item in list(pattern.get("counterexamples") or []))
    for linked_id in linked_ids:
        linked = records.get(linked_id)
        if not linked:
            continue
        linked_type = str(linked.get("type") or "")
        linked_status = str(linked.get("status") or "")
        if linked_type == "contradiction_log":
            return True
        if linked_type == "skeptical_review" and str(linked.get("risk") or "") == "high" and not bool(linked.get("approved", False)):
            return True
        if linked_type == "claim" and linked_status in {"disputed", "rejected"} and float(linked.get("confidence") or 0.0) < 0.5:
            return True
    return False


def _output_path(vault: Path, task: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    if task == "contradict":
        return vault / "contradictions" / f"dreamer-contradictions-{stamp}.md"
    return vault / "reports" / f"dreamer-{task}-{stamp}.md"


def _apply_task_side_effect(vault: Path, task: str, bundle: str, response: dict[str, Any]) -> Path | None:
    if task == "contradict":
        return _write_contradiction_log(vault, bundle, response)
    return None


def _write_contradiction_log(vault: Path, bundle: str, response: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    out = vault / "contradictions" / f"dreamer-contradictions-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Contradiction Log",
        "",
        "## Dreamer Findings",
        "",
        "```json",
        json.dumps(response, indent=2, ensure_ascii=True),
        "```",
        "",
        "## Bundle",
        "",
        bundle.strip(),
    ]
    write_markdown(
        out,
        {
            "id": f"contradiction_log.{stamp}",
            "type": "contradiction_log",
            "created": today_iso(),
            "updated": today_iso(),
            "status": "active",
            "significance": "medium",
            "domain_primary": "cross_arena",
            "domain_secondary": [],
            "privacy": "personal",
            "disclosure": "private",
            "summary": render_for_display("Dreamer contradiction log", vault),
            "links": [],
            "confidence": "low",
            "confidence_basis": "Dreamer contradiction analysis",
            "last_confirmed": today_iso(),
            "review_after": today_iso(),
        },
        render_for_display("\n".join(lines) + "\n", vault),
    )
    return out


def _render_report(out: Path, task: str, bundle: str, response: dict[str, Any], artifact_path: Path | None) -> None:
    vault = out.parents[1]
    stamp = out.stem.split("-")[-1]
    frontmatter = {
        "id": f"dreamer.{task}.{stamp}",
        "type": "report",
        "created": today_iso(),
        "updated": today_iso(),
        "status": "active",
        "significance": "medium",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": render_for_display(f"Dreamer {task.replace('_', ' ')} report", vault),
        "links": [str(artifact_path)] if artifact_path else [],
        "confidence": "low",
        "confidence_basis": f"Dreamer {task} analysis",
        "last_confirmed": today_iso(),
        "review_after": today_iso(),
        "task": task,
    }
    body = f"""# Dreamer {task.replace('_', ' ').title()}

## Response

```json
{json.dumps(response, indent=2, ensure_ascii=True)}
```

## Bundle

{bundle.strip()}
"""
    write_markdown(out, frontmatter, render_for_display(body, vault))
