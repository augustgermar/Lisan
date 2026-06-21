from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..agents import AnalystAgent, SkepticAgent
from ..frontmatter import load_markdown, write_markdown
from ..paths import vault_root
from ..utils import slugify, today_iso
from .deixis import render_for_display
from .epistemic import (
    canonical_pattern_status,
    load_existing_patterns,
    pattern_conflicts_with_existing,
    pattern_contains_diagnostic_language,
    pattern_counterexample_search_result,
    pattern_is_too_broad,
)
from .record_factory import new_pattern, new_skeptical_review


@dataclass(slots=True)
class AnalystRunResult:
    report_path: Path
    pattern_paths: list[Path]
    review_paths: list[Path]
    response: dict[str, Any]


def run_analyst_scan(
    vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AnalystRunResult:
    vault = vault or vault_root()
    bundle = build_analyst_bundle(vault)
    agent = AnalystAgent(vault=vault)
    if provider or model:
        response = agent.run_json(bundle, significance="high", provider=provider, model=model)
    else:
        response = json.loads(agent.fallback_output(bundle))
    pattern_paths: list[Path] = []
    review_paths: list[Path] = []
    existing_patterns = load_existing_patterns(vault)
    for pattern in response.get("patterns") or []:
        if not isinstance(pattern, dict):
            continue
        created = _materialize_pattern(vault, bundle, pattern, existing_patterns)
        if created is None:
            continue
        pattern_paths.append(created.path)
        existing_patterns.append(
            {
                "status": "active_hypothesis",
                "pattern_type": str(pattern.get("pattern_type") or "other"),
                "hypothesis": str(pattern.get("hypothesis") or ""),
            }
        )
        review = review_pattern(vault, created.path, pattern, provider=provider, model=model)
        if review is not None:
            review_paths.append(review.path)
    report_path = _write_report(vault, response, pattern_paths, review_paths)
    return AnalystRunResult(report_path=report_path, pattern_paths=pattern_paths, review_paths=review_paths, response=response)


def build_analyst_bundle(vault: Path) -> str:
    sections: list[str] = ["# Analyst Bundle", ""]
    sources = [
        ("Episodes", vault / "episodes"),
        ("Claims", vault / "claims"),
        ("Evidence", vault / "evidence" / "records"),
        ("Patterns", vault / "patterns"),
        ("Skeptical Reviews", vault / "reviews"),
        ("Contradictions", vault / "contradictions"),
        ("Dreamer Summaries", vault / "reports"),
    ]
    for heading, root in sources:
        if not root.exists():
            continue
        sections.append(f"## {heading}")
        for path in sorted(root.rglob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            if heading == "Dreamer Summaries" and not str(doc.frontmatter.get("id", "")).startswith("dreamer."):
                continue
            sections.append(f"### {path.relative_to(vault)}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _materialize_pattern(vault: Path, bundle: str, pattern: dict[str, Any], existing_patterns: list[dict[str, Any]]):
    try:
        hypothesis = str(pattern.get("hypothesis") or "").strip()
        pattern_type = str(pattern.get("pattern_type") or "other")
        if not hypothesis:
            return None
        support = list(pattern.get("supporting_records") or [])
        support_count = len(support)
        if support_count < 2:
            return None
        if pattern_is_too_broad(hypothesis) or pattern_contains_diagnostic_language(hypothesis):
            return None
        if pattern_conflicts_with_existing(hypothesis, pattern_type, existing_patterns):
            return None
        counterexample_search = pattern.get("counterexample_search")
        if not isinstance(counterexample_search, dict) or not bool(counterexample_search.get("performed", False)):
            counterexample_search = pattern_counterexample_search_result(bundle, hypothesis, pattern_type, support)
        counterexamples = list(pattern.get("counterexamples") or [])
        if not counterexamples:
            counterexamples = list(counterexample_search.get("counterexamples") or [])
        return new_pattern(
            vault=vault,
            pattern_type=pattern_type,
            hypothesis=hypothesis,
            supporting_records=support,
            counterexamples=counterexamples,
            alternative_explanations=list(pattern.get("alternative_explanations") or []),
            confidence=float(pattern.get("confidence") or 0.35),
            status=canonical_pattern_status(str(pattern.get("status") or "candidate")),
            first_seen=str(pattern.get("first_seen") or today_iso()),
            last_reviewed=str(pattern.get("last_reviewed") or today_iso()),
            predictions=list(pattern.get("predictions") or []),
            review_notes=str(pattern.get("review_notes") or ""),
            evidence_needed=list(pattern.get("evidence_needed") or []),
            counterexample_search=counterexample_search,
            strength_override=bool(pattern.get("strength_override", False)),
            integration_override=pattern.get("integration_override") if isinstance(pattern.get("integration_override"), dict) else None,
        )
    except (FileExistsError, ValueError):
        return None


def review_pattern(vault: Path, pattern_path: Path, pattern: dict[str, Any] | None = None, provider: str | None = None, model: str | None = None):
    doc = load_markdown(pattern_path)
    pattern = pattern or {}
    skeptical = SkepticAgent(vault=vault).run_json(
        json.dumps(
            {
                "frontmatter": doc.frontmatter,
                "body": doc.body,
                "pattern": pattern,
            },
            indent=2,
            ensure_ascii=True,
        ),
        significance="medium",
        provider=provider,
        model=model,
    ) if (provider or model) else json.loads(
        SkepticAgent(vault=vault).fallback_output(
            json.dumps(
                {
                    "frontmatter": doc.frontmatter,
                    "body": doc.body,
                    "pattern": pattern,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
    )
    try:
        approved_for_dreamer = bool(skeptical.get("approved_for_dreamer", False))
        pattern_status = str(skeptical.get("pattern_status") or doc.frontmatter.get("status") or "skeptic_reviewed")
        review = new_skeptical_review(
            vault=vault,
            reviewed_record_id=str(doc.frontmatter.get("id", pattern_path.stem)),
            reviewed_record_type="pattern",
            summary=str(skeptical.get("summary") or doc.frontmatter.get("summary") or pattern_path.stem),
            approved=bool(skeptical.get("approved", False)),
            risk=str(skeptical.get("risk", "medium")),
            recommended_action=str(skeptical.get("recommended_action", "revise")),
            issues=list(skeptical.get("issues") or []),
            priority_questions=list(skeptical.get("priority_questions") or []),
            alternative_hypotheses=list(skeptical.get("alternative_hypotheses") or []),
            evidence_needed=list(skeptical.get("evidence_needed") or []),
            claim_updates=list(skeptical.get("claim_updates") or []),
            confidence_adjustments=list(skeptical.get("confidence_adjustments") or []),
            reasoning_errors=list(skeptical.get("reasoning_errors") or []),
            approved_for_dreamer=approved_for_dreamer,
            pattern_status=pattern_status,
            counterexample_search=dict(skeptical.get("counterexample_search") or {}),
            significance="medium",
        )
    except (FileExistsError, ValueError):
        return None
    try:
        updated = dict(doc.frontmatter)
        updated["status"] = pattern_status if pattern_status in {"candidate", "active_hypothesis", "skeptic_reviewed", "supported", "integrated", "disputed", "stale", "rejected", "retired"} else ("disputed" if not bool(skeptical.get("approved", False)) else "skeptic_reviewed")
        updated["updated"] = today_iso()
        updated["last_reviewed"] = today_iso()
        updated["counterexample_search"] = dict(skeptical.get("counterexample_search") or updated.get("counterexample_search") or {})
        updated["strength_override"] = bool(updated.get("strength_override", False))
        if bool(skeptical.get("approved_for_dreamer", False)) and updated["status"] in {"skeptic_reviewed", "supported"}:
            updated["status"] = "supported"
        write_markdown(pattern_path, updated, doc.body)
    except Exception:
        pass
    return review


def _write_report(vault: Path, response: dict[str, Any], pattern_paths: list[Path], review_paths: list[Path]) -> Path:
    today = today_iso()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    path = vault / "reports" / f"analyst-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": f"report.analyst.{stamp}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": render_for_display(str(response.get("summary") or "Analyst longitudinal pattern report"), vault),
        "links": [str(p.relative_to(vault)) for p in pattern_paths + review_paths],
        "confidence": "low",
        "confidence_basis": "Analyst longitudinal scan",
        "last_confirmed": today,
        "review_after": today,
        "task": "analyst",
    }
    body = f"""# Analyst Longitudinal Report

## Response

```json
{json.dumps(response, indent=2, ensure_ascii=True)}
```

## Patterns

{chr(10).join(f"- `{p.relative_to(vault)}`" for p in pattern_paths) or "- None"}

## Reviews

{chr(10).join(f"- `{p.relative_to(vault)}`" for p in review_paths) or "- None"}

## Bundle Summary

Analyst scan completed over episodes, claims, evidence, skeptical reviews, contradictions, and Dreamer summaries.
"""
    write_markdown(path, frontmatter, render_for_display(body, vault))
    return path
