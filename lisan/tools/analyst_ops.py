from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..agents import AnalystAgent, SelfAnalystAgent, SkepticAgent
from ..config import load_config
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
from .checkin import observation_summary_for_entity


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
    agent = AnalystAgent(vault=vault)
    pattern_paths: list[Path] = []
    review_paths: list[Path] = []
    existing_patterns = load_existing_patterns(vault)
    subjects = eligible_analyst_subjects(vault)
    responses: list[dict[str, Any]] = []
    # A vault without person entities may still contain general longitudinal
    # evidence. Keep that existing analyst behavior for those vaults; once
    # people are present, Psyche analysis is strictly per-person and gated.
    person_files = list((vault / "entities" / "people").glob("*.md")) if (vault / "entities" / "people").exists() else []
    bundles = [(None, build_analyst_bundle(vault))] if not person_files else [
        (subject, build_analyst_bundle(vault, subject)) for subject in subjects
    ]
    for subject, bundle in bundles:
        if provider or model:
            response = agent.run_json(bundle, significance="high", provider=provider, model=model)
        else:
            response = json.loads(agent.fallback_output(bundle))
        responses.append(response)
        for pattern in response.get("patterns") or []:
            if not isinstance(pattern, dict):
                continue
            created = _materialize_pattern(vault, bundle, pattern, existing_patterns)
            if created is None:
                continue
            if subject is not None:
                _append_pattern_link(created.path, subject["id"])
            pattern_paths.append(created.path)
            existing_patterns.append({"status": "active_hypothesis", "pattern_type": str(pattern.get("pattern_type") or "other"), "hypothesis": str(pattern.get("hypothesis") or "")})
            review = review_pattern(vault, created.path, pattern, provider=provider, model=model)
            if review is not None:
                review_paths.append(review.path)
    if not responses:
        responses = [{
            "summary": "No person has met the Psyche analyst evidence threshold yet.",
            "patterns": [],
            "notes": ["Automatic analysis requires 5 check-ins across 3 distinct weeks."],
        }]
    response = {
        "summary": "Per-person Psyche scan completed." if subjects else responses[0].get("summary", "No eligible Psyche subjects."),
        "patterns": [p for item in responses for p in (item.get("patterns") or [])],
        "notes": [note for item in responses for note in (item.get("notes") or [])],
        "subjects": [subject["id"] for subject in subjects],
    }
    report_path = _write_report(vault, response, pattern_paths, review_paths)
    return AnalystRunResult(report_path=report_path, pattern_paths=pattern_paths, review_paths=review_paths, response=response)


def _append_pattern_link(path: Path, entity_id: str) -> None:
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    links = list(fm.get("links") or [])
    if entity_id not in links:
        links.append(entity_id)
        fm["links"] = links
        write_markdown(path, fm, doc.body)


def eligible_analyst_subjects(vault: Path) -> list[dict[str, Any]]:
    """Return people with enough dated check-ins for a conservative scan."""
    settings = (load_config().get("psyche") or {})
    minimum_observations = max(1, int(settings.get("analyst_min_observations", 5)))
    minimum_weeks = max(1, int(settings.get("analyst_min_weeks", 3)))
    people = vault / "entities" / "people"
    eligible: list[dict[str, Any]] = []
    if not people.exists():
        return eligible
    for path in sorted(people.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("subtype") or fm.get("kind") or "") != "person":
            continue
        entity_id = str(fm.get("id") or path.stem)
        summary = observation_summary_for_entity(vault, entity_id)
        if summary["observation_count"] >= minimum_observations and summary["distinct_weeks"] >= minimum_weeks:
            eligible.append({"id": entity_id, "canonical_name": str(fm.get("canonical_name") or path.stem), "summary": summary})
    return eligible


def build_analyst_bundle(vault: Path, subject: dict[str, Any] | None = None) -> str:
    sections: list[str] = ["# Analyst Bundle", ""]
    subject_id = str(subject.get("id")) if subject else None
    if subject:
        sections.extend(["## Analysis Subject", f"- id: {subject_id}", f"- name: {subject.get('canonical_name')}", f"- derived_observation_summary: {json.dumps(subject.get('summary') or {}, sort_keys=True)}", ""])
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
            if subject_id and heading not in {"Dreamer Summaries"}:
                links = doc.frontmatter.get("links") or []
                actors = doc.frontmatter.get("actors") or []
                if subject_id not in links and subject.get("canonical_name") not in actors:
                    continue
            if heading == "Dreamer Summaries" and not str(doc.frontmatter.get("id", "")).startswith("dreamer."):
                continue
            sections.append(f"### {path.relative_to(vault)}")
            if heading == "Dreamer Summaries":
                # Summaries, as the heading says. Reading whole dreamer
                # reports to get at their conclusions pulled 141 MB of
                # archived bundles into this prompt — 96% of it, and every
                # byte a duplicate of records already listed above.
                sections.append(_dreamer_summary(doc))
            else:
                sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def _dreamer_summary(doc: Any) -> str:
    """A dreamer report's conclusions: its frontmatter summary plus the
    response block, never the archived bundle."""
    parts = [str(doc.frontmatter.get("summary") or "").strip()]
    match = re.search(r"## Response\n\n```json\n(.*?)\n```", doc.body, re.DOTALL)
    if match:
        parts.append(match.group(1).strip())
    return "\n\n".join(part for part in parts if part)



def independent_support_count(vault: Path, support: list[Any]) -> int:
    """How many distinct sources these supporting references actually represent.

    References arrive as record ids (canonicalized at write time), so each is
    resolved to its record and grouped by lineage — the document, artifact or
    ingestion batch it came from. Sibling chunks of one document collapse to
    one. A reference that cannot be resolved counts as its own lineage rather
    than as nothing: an unresolvable citation should not silently *help* a
    hypothesis clear the bar, but neither should it be treated as corroboration
    it cannot provide.
    """
    from .origin import independent_lineages
    from .record_refs import build_reference_index, resolve_reference

    if not support:
        return 0
    index = build_reference_index(vault)
    frontmatters: list[dict[str, Any]] = []
    unresolvable = 0
    for reference in support:
        resolution = resolve_reference(reference, vault, index)
        target = resolution.target if (resolution.ok or resolution.repairable) else ""
        path = index.ids.get(target) if target else None
        if path is None:
            unresolvable += 1
            continue
        try:
            frontmatters.append(load_markdown(vault / path).frontmatter)
        except Exception:
            unresolvable += 1
    return len(independent_lineages(frontmatters)) + unresolvable


def _materialize_pattern(vault: Path, bundle: str, pattern: dict[str, Any], existing_patterns: list[dict[str, Any]]):
    try:
        hypothesis = str(pattern.get("hypothesis") or "").strip()
        pattern_type = str(pattern.get("pattern_type") or "other")
        if not hypothesis:
            return None
        support = list(pattern.get("supporting_records") or [])
        # The evidence gate counts *sources*, not records.
        #
        # `len(support) >= 2` asked whether someone wrote two things down. The
        # question it was written to answer is whether two sources agree — and
        # those differ badly once a document is chunked. Measured on a
        # production vault 2026-07-30: 339 of 340 knowledge records carry a
        # `source_document`, and one document had produced 38 chunks. Under the
        # old bar that single document could found a pattern about a person
        # nineteen times over, by itself, and the citation list would look
        # thorough. One of those groups was an 8-chunk document about family
        # members.
        #
        # This is the "manufactured corroboration" laundering channel: many
        # untrusted copies faking consensus. The defence is to require distinct
        # lineages, with the rule that an item cannot corroborate itself.
        independent = independent_support_count(vault, support)
        if independent < 2:
            if support:
                from .log import get_logger

                get_logger(vault).info(
                    "analyst pattern refused: %d supporting record(s) resolve to "
                    "%d independent source(s) — %s",
                    len(support), independent, hypothesis[:120],
                )
            return None
        if pattern_is_too_broad(hypothesis):
            return None
        if pattern_contains_diagnostic_language(hypothesis):
            # Loud, never silent (owner policy): a refused hypothesis is a
            # visible event in the vault log, not a record that quietly
            # never appears. The gate is owner-configurable
            # (psyche.banned_hypothesis_terms; [] disables it).
            from .log import get_logger

            get_logger(vault).warning(
                f"analyst hypothesis refused by the language gate: {hypothesis[:160]!r}"
            )
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


# ── §4.4: Agent self-analysis ──────────────────────────────────────────────────

_SELF_EVAL_HISTORY_REL = "reports/self-eval-history.jsonl"

_MIN_SELF_EPISODES = 5
_MIN_SELF_EVAL_ENTRIES = 3


def find_self_entity(vault: Path) -> dict[str, Any] | None:
    """Locate the agent's own entity record under entities/agents/."""
    agents_dir = vault / "entities" / "agents"
    if not agents_dir.exists():
        return None
    for path in sorted(agents_dir.glob("*.md")):
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        if str(fm.get("software") or "").lower() == "lisan":
            return {
                "id": str(fm.get("id") or path.stem),
                "canonical_name": str(fm.get("canonical_name") or path.stem),
                "path": path,
            }
    return None


def self_analysis_eligible(vault: Path, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return the self-entity dict if enough operational evidence exists,
    else None.  Evidence = self-episodes + self-eval history entries."""
    self_ent = find_self_entity(vault)
    if self_ent is None:
        return None
    episodes_dir = vault / "self" / "episodes"
    episode_count = len(list(episodes_dir.glob("*.md"))) if episodes_dir.exists() else 0
    history_path = vault / _SELF_EVAL_HISTORY_REL
    eval_count = 0
    if history_path.exists():
        try:
            eval_count = sum(1 for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            pass
    if episode_count < _MIN_SELF_EPISODES and eval_count < _MIN_SELF_EVAL_ENTRIES:
        return None
    self_ent["episode_count"] = episode_count
    self_ent["eval_count"] = eval_count
    return self_ent


def build_self_analyst_bundle(vault: Path, *, db_path: Path | None = None) -> str:
    """Assemble the corpus for self-analysis: first-person episodes,
    self-eval history, job outcomes, deviation findings.
    NOT owner check-ins — those never mention the agent."""
    from .db import connect as _db_connect
    from ..paths import sqlite_path

    sections: list[str] = ["# Self-Analyst Bundle", ""]
    self_ent = find_self_entity(vault)
    if self_ent:
        sections.extend([
            "## Analysis Subject",
            f"- id: {self_ent['id']}",
            f"- name: {self_ent['canonical_name']}",
            f"- kind: agent (self)",
            "",
        ])

    # First-person episodes
    episodes_dir = vault / "self" / "episodes"
    if episodes_dir.exists():
        sections.append("## Self-Episodes")
        for path in sorted(episodes_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            sections.append(f"### {path.relative_to(vault)}")
            sections.append(text)
            sections.append("")

    # Self-eval history (dimension scores over time)
    history_path = vault / _SELF_EVAL_HISTORY_REL
    if history_path.exists():
        sections.append("## Self-Evaluation History")
        try:
            lines = [l for l in history_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            for line in lines[-20:]:
                entry = json.loads(line)
                dims = entry.get("dimensions") or {}
                dim_summary = ", ".join(
                    f"{d}: {s.get('mean', '?')}" for d, s in dims.items()
                )
                sections.append(
                    f"- {entry.get('date', '?')}: overall={entry.get('overall_mean', '?')} "
                    f"({dim_summary})"
                )
        except Exception:
            sections.append("- (unreadable)")
        sections.append("")

    # Self-eval reports
    reports_dir = vault / "reports"
    if reports_dir.exists():
        sections.append("## Self-Evaluation Reports")
        for path in sorted(reports_dir.glob("self-eval-*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            sections.append(f"### {path.relative_to(vault)}")
            sections.append(_dreamer_summary(doc))
            sections.append("")

    # Job outcome summary (last 100 finished jobs)
    db = sqlite_path(vault) if db_path is None else db_path
    if db.exists():
        sections.append("## Job Outcomes (recent)")
        try:
            conn = _db_connect(db)
            rows = conn.execute(
                "SELECT job_type, status, COUNT(*) as cnt "
                "FROM jobs WHERE status IN ('succeeded', 'failed') "
                "GROUP BY job_type, status ORDER BY job_type, status"
            ).fetchall()
            conn.close()
            for job_type, status, cnt in rows:
                sections.append(f"- {job_type}: {status}={cnt}")
        except Exception:
            sections.append("- (query failed)")
        sections.append("")

    # Active deviation loops (the agent's current aches)
    loops_dir = vault / "open_loops"
    if loops_dir.exists():
        sections.append("## Active Deviation Loops")
        for path in sorted(loops_dir.glob("*deviation*.md")):
            try:
                doc = load_markdown(path)
                fm = doc.frontmatter
            except Exception:
                continue
            if str(fm.get("status") or "") != "active":
                continue
            if str(fm.get("origin") or "") != "self":
                continue
            sections.append(f"### {path.relative_to(vault)}")
            sections.append(
                f"- fingerprint: {fm.get('deviation_fingerprint', '?')}"
            )
            sections.append(f"- summary: {fm.get('summary', '?')}")
            sections.append("")

    # Existing self-patterns (to avoid duplicates)
    sections.append("## Existing Self-Patterns")
    patterns_dir = vault / "patterns"
    found_any = False
    if patterns_dir.exists():
        self_id = self_ent["id"] if self_ent else ""
        for path in sorted(patterns_dir.glob("*.md")):
            try:
                fm = load_markdown(path).frontmatter
            except Exception:
                continue
            links = fm.get("links") or []
            if self_id and self_id not in links:
                continue
            found_any = True
            sections.append(f"- {fm.get('hypothesis', path.stem)} (status={fm.get('status', '?')})")
    if not found_any:
        sections.append("- None yet")
    sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def run_self_analyst_scan(
    vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    *,
    db_path: Path | None = None,
) -> AnalystRunResult:
    """Run the agent self-analysis pass (WO-PSYCHE §4.4)."""
    vault = vault or vault_root()
    self_ent = self_analysis_eligible(vault, db_path=db_path)
    if self_ent is None:
        return AnalystRunResult(
            report_path=Path("/dev/null"),
            pattern_paths=[],
            review_paths=[],
            response={
                "summary": "Self-analysis not yet eligible: insufficient self-episodes or self-eval history.",
                "patterns": [],
                "notes": [
                    f"Need {_MIN_SELF_EPISODES} self-episodes or {_MIN_SELF_EVAL_ENTRIES} self-eval entries.",
                ],
            },
        )

    agent = SelfAnalystAgent(vault=vault)
    bundle = build_self_analyst_bundle(vault, db_path=db_path)
    existing_patterns = load_existing_patterns(vault)
    pattern_paths: list[Path] = []
    review_paths: list[Path] = []

    if provider or model:
        response = agent.run_json(bundle, significance="high", provider=provider, model=model)
    else:
        response = json.loads(agent.fallback_output(bundle))

    self_entity_id = self_ent["id"]
    for pattern in response.get("patterns") or []:
        if not isinstance(pattern, dict):
            continue
        created = _materialize_pattern(vault, bundle, pattern, existing_patterns)
        if created is None:
            continue
        _append_pattern_link(created.path, self_entity_id)
        pattern_paths.append(created.path)
        existing_patterns.append({
            "status": "active_hypothesis",
            "pattern_type": str(pattern.get("pattern_type") or "other"),
            "hypothesis": str(pattern.get("hypothesis") or ""),
        })
        review = review_pattern(vault, created.path, pattern, provider=provider, model=model)
        if review is not None:
            review_paths.append(review.path)

    _emit_self_pattern_loops(vault, pattern_paths, db_path=db_path)

    report_path = _write_self_analyst_report(vault, response, pattern_paths, review_paths)
    response["report"] = str(report_path.relative_to(vault))
    response["pattern_count"] = len(pattern_paths)
    response["review_count"] = len(review_paths)

    return AnalystRunResult(
        report_path=report_path,
        pattern_paths=pattern_paths,
        review_paths=review_paths,
        response=response,
    )


def _emit_self_pattern_loops(
    vault: Path,
    pattern_paths: list[Path],
    *,
    db_path: Path | None,
) -> list[str]:
    """A confirmed negative self-pattern emits an origin:self improvement loop
    through the deviation seam, feeding into the self-repair pipeline."""
    from .deviations import _emit, deviations_config

    deviations: list[dict[str, Any]] = []
    for path in pattern_paths:
        try:
            fm = load_markdown(path).frontmatter
        except Exception:
            continue
        status = str(fm.get("status") or "")
        if status not in ("supported", "active_hypothesis"):
            continue
        hypothesis = str(fm.get("hypothesis") or "")
        fingerprint = f"self-pattern-{slugify(hypothesis)[:40]}"
        rel = str(path.relative_to(vault))
        deviations.append({
            "klass": "self_eval",
            "fingerprint": fingerprint,
            "summary": f"Self-analysis pattern: {hypothesis}",
            "links": [rel],
        })

    if not deviations:
        return []
    return _emit(vault, deviations, deviations_config(None), date.today(), db_path=db_path)


def _write_self_analyst_report(
    vault: Path,
    response: dict[str, Any],
    pattern_paths: list[Path],
    review_paths: list[Path],
) -> Path:
    today = today_iso()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    path = vault / "reports" / f"self-analyst-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": f"report.self-analyst.{stamp}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": render_for_display(
            str(response.get("summary") or "Self-analyst longitudinal pattern report"),
            vault,
        ),
        "links": [str(p.relative_to(vault)) for p in pattern_paths + review_paths],
        "confidence": "low",
        "confidence_basis": "Self-analyst longitudinal scan",
        "last_confirmed": today,
        "review_after": today,
        "task": "self_analyst",
    }
    body = f"""# Self-Analyst Longitudinal Report

## Response

```json
{json.dumps(response, indent=2, ensure_ascii=True)}
```

## Patterns

{chr(10).join(f"- `{p.relative_to(vault)}`" for p in pattern_paths) or "- None"}

## Reviews

{chr(10).join(f"- `{p.relative_to(vault)}`" for p in review_paths) or "- None"}

## Bundle Summary

Self-analyst scan completed over first-person episodes, self-evaluation history, job outcomes, and deviation loops.
"""
    write_markdown(path, frontmatter, render_for_display(body, vault))
    return path
