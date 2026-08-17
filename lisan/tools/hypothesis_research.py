"""Bounded external cross-checks for stored hypotheses.

Research is evidence for review, not an automatic promotion mechanism. The
stored hypothesis is never rewritten from a web result. A single clear
finding produces a review report; no finding or multiple candidate findings
creates an owner-question loop so Telegram can surface the ambiguity through
the existing drive.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..config import load_config
from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .action_policy import action_allowed
from .record_factory import new_open_loop
from .research import SourceProvider, search_published_sources
from .retrieval import retrieve_context


def investigate_hypothesis(
    *,
    vault: Path,
    hypothesis: str,
    question: str,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    published_providers: Iterable[SourceProvider] = (),
) -> dict[str, Any]:
    """Cross-reference one stored hypothesis with internal and web sources.

    The result is deliberately a report. It never changes the hypothesis or
    turns a web excerpt into a fact. ``question`` is the bounded research
    query; callers should not use this as a general web crawler.
    """
    cfg = config or load_config()
    if not action_allowed("enrich", cfg):
        return {"status": "blocked", "reason": "enrichment action tier is not enabled"}
    query = str(question or "").strip()
    if not query:
        return {"status": "refused", "reason": "a focused research question is required"}

    target_path = _resolve_hypothesis(vault, hypothesis)
    if target_path is None:
        return {"status": "refused", "reason": f"hypothesis not found: {hypothesis}"}
    target = load_markdown(target_path)
    target_id = str(target.frontmatter.get("id") or target_path.stem)

    internal: list[dict[str, str]] = []
    try:
        context = retrieve_context(query=query, vault=vault, db_path=db_path)
        for item in list(context.loaded)[:5]:
            if str(getattr(item, "id", "") or "").startswith("prediction."):
                continue
            internal.append({
                "id": str(getattr(item, "id", "") or ""),
                "path": str(getattr(item, "path", "") or ""),
                "summary": str(getattr(item, "summary", "") or "")[:500],
            })
    except Exception:
        internal = []

    web_findings = search_published_sources(
        query,
        providers=published_providers,
        max_results_per_source=int((cfg.get("enrichment") or {}).get("max_candidates_per_source", 5)),
    )
    web = [
        {
            "source": finding.source,
            "url": finding.locator,
            "title": finding.title,
            "excerpt": finding.excerpt[:1200],
            "publisher": finding.publisher,
            "published_at": finding.published_at,
            "retrieved_at": finding.retrieved_at,
            "confidence": finding.confidence,
        }
        for finding in web_findings
    ]
    if len(web) == 1:
        status = "candidate_found"
    elif len(web) == 0:
        status = "needs_owner" if not internal else "internal_only"
    else:
        status = "needs_owner"

    report = _write_report(vault, target_id, target_path, query, status, internal, web)
    loop_path = None
    if status == "needs_owner":
        loop_path = _owner_question(vault, target_id, target_path, query, report, len(web))
    return {
        "status": status,
        "report": str(report),
        "hypothesis": target_id,
        "internal_findings": len(internal),
        "web_findings": len(web),
        "owner_loop": str(loop_path) if loop_path else "",
    }


def _resolve_hypothesis(vault: Path, value: str) -> Path | None:
    candidate = Path(str(value or "").strip())
    if candidate.is_absolute() and candidate.exists():
        return candidate
    if (vault / str(value)).exists():
        return vault / str(value)
    for root in (vault / "patterns", vault / "claims", vault / "knowledge"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                if str(load_markdown(path).frontmatter.get("id") or "") == str(value):
                    return path
            except Exception:
                continue
    return None


def _write_report(
    vault: Path, target_id: str, target_path: Path, query: str, status: str,
    internal: list[dict[str, str]], web: list[dict[str, Any]],
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    path = vault / "reports" / f"hypothesis-research-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "id": f"report.hypothesis-research.{stamp}",
        "type": "report",
        "created": today_iso(),
        "updated": today_iso(),
        "status": "active",
        "significance": "medium",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "disclosure": "private",
        "summary": f"External cross-check of hypothesis {target_id}",
        "links": [target_id, str(target_path.relative_to(vault))],
        "confidence": "low",
        "confidence_basis": "Bounded internal and published-source comparison; no automatic promotion",
        "last_confirmed": today_iso(),
        "review_after": today_iso(),
        "hypothesis_id": target_id,
        "research_query": query,
        "research_status": status,
        "internal_findings": internal,
        "web_findings": web,
    }
    body = (
        "# Hypothesis Research Report\n\n"
        f"- Hypothesis: `{target_id}`\n- Query: {query}\n- Status: **{status}**\n\n"
        "The original hypothesis was not rewritten. These are reviewable evidence pointers.\n\n"
        "## Internal findings\n\n"
        + ("\n".join(f"- `{item['id']}` — {item['summary']}" for item in internal) or "- None")
        + "\n\n## Published findings\n\n"
        + ("\n".join(f"- [{item['title'] or item['url']}]({item['url']}) — {item['excerpt']}" for item in web) or "- None")
        + "\n"
    )
    write_markdown(path, fm, body)
    return path


def _owner_question(vault: Path, target_id: str, target_path: Path, query: str, report: Path, web_count: int) -> Path:
    question = (
        f"I cross-referenced hypothesis {target_id} using the vault and published web sources, "
        f"but found {web_count} web candidates. Which interpretation should I retain, or should "
        "this hypothesis remain unresolved?"
    )
    created = new_open_loop(
        vault,
        title=f"Clarify research for {target_id}",
        domain_primary="cross_arena",
        significance="medium",
        summary=question,
        links=[target_id, str(target_path.relative_to(vault)), str(report.relative_to(vault))],
        confidence="low",
        confidence_basis="External cross-check requires owner judgment",
        next_action="Ask the owner through the private Telegram callback and record the answer.",
        owner="user",
    )
    doc = load_markdown(created.path)
    fm = dict(doc.frontmatter)
    fm.update({
        "origin": "research",
        "next_action": "ask_owner",
        "owner_question": question,
        "research_report": str(report.relative_to(vault)),
        "research_query": query,
    })
    write_markdown(created.path, fm, doc.body)
    return created.path
