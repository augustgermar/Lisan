"""Contract-driven domain knowledge librarian.

The contract is the durable permission record.  A build may search broadly,
but it only promotes material from origins explicitly approved for that
domain; everything else remains unverified and is not silently upgraded.
"""
from __future__ import annotations

import tempfile
import hashlib
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import slugify, today_iso
from .ingest import ingest_reference_sources
from .research import SourceFinding, installed_published_providers, search_published_sources
from .source_tiers import SOURCE_TIERS, normalize_source_tier, now_utc, origin_for_url, origin_matches
from .record_factory import supersede_record


def contract_path(vault: Path, domain: str) -> Path:
    return vault / "domains" / slugify(domain) / "sourcing-contract.md"


def create_contract(
    vault: Path,
    domain: str,
    *,
    domain_tag: str | None = None,
    reputability_bar: str = "Prefer current, attributable, primary or official sources; flag stale or ambiguous material.",
    owner: str = "owner",
) -> Path:
    path = contract_path(vault, domain)
    if path.exists():
        return path
    today = today_iso()
    fm = {
        "id": f"domain_contract.{slugify(domain)}",
        "type": "domain_contract",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "medium",
        "domain_primary": domain_tag or slugify(domain),
        "domain_secondary": [],
        "privacy": "personal",
        "summary": f"Sourcing contract for {domain}",
        "links": [],
        "confidence": "high",
        "confidence_basis": "Owner-approved contract",
        "last_confirmed": today,
        "review_after": today,
        "domain_name": domain,
        "approved_by": owner,
        "approved_at": today,
        "reputability_bar": reputability_bar,
        "approved_origins": [],
        "rejected_origins": [],
        "contract_version": 1,
    }
    body = f"""# Sourcing contract: {domain}

This contract is committed before autonomous ingestion. Origin authority comes
from this file, not from wording found in a web page.

## Reputability bar

{reputability_bar}

## Approved origins

No origins have been approved yet. Add one only after owner confirmation.

## Rejected or down-tiered origins

None recorded.
"""
    write_markdown(path, fm, body)
    return path


def load_contract(vault: Path, domain: str) -> dict[str, Any]:
    path = contract_path(vault, domain)
    if not path.exists():
        raise FileNotFoundError(f"No sourcing contract exists for {domain}; create one before building")
    return dict(load_markdown(path).frontmatter)


def approve_origin(
    vault: Path,
    domain: str,
    origin: str,
    *,
    tier: str = "primary",
    rationale: str = "",
    approved_by: str = "owner",
) -> Path:
    tier = normalize_source_tier(tier)
    if tier not in {"primary", "official-secondary"}:
        raise ValueError("Only primary and official-secondary origins can be approved in the contract")
    path = contract_path(vault, domain)
    if not path.exists():
        create_contract(vault, domain)
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    origins = [dict(item) for item in (fm.get("approved_origins") or []) if isinstance(item, dict)]
    clean = origin_for_url(origin)
    if not any(str(item.get("origin")) == clean for item in origins):
        origins.append({"origin": clean, "tier": tier, "rationale": rationale, "approved_by": approved_by, "approved_at": today_iso()})
    fm["approved_origins"] = origins
    fm["updated"] = today_iso()
    fm["contract_version"] = int(fm.get("contract_version") or 1) + 1
    body = doc.body.rstrip() + f"\n\nOrigin approved: `{clean}` as `{tier}` — {rationale or 'owner-approved origin'}.\n"
    write_markdown(path, fm, body)
    return path


def _approved_tier(contract: dict[str, Any], url: str) -> str | None:
    for item in contract.get("approved_origins") or []:
        if isinstance(item, dict) and origin_matches(str(item.get("origin") or ""), url):
            return normalize_source_tier(str(item.get("tier") or ""))
    return None


def build_domain(
    vault: Path,
    domain: str,
    query: str,
    *,
    db_path: Path | None = None,
    limit: int = 5,
    domain_tag: str | None = None,
) -> dict[str, Any]:
    contract = load_contract(vault, domain)
    config = load_config()
    findings = search_published_sources(query, providers=installed_published_providers(config=config), max_results_per_source=limit)
    approved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        tier = _approved_tier(contract, finding.locator)
        if tier is None:
            skipped.append({"url": finding.locator, "reason": "origin not approved by contract"})
            continue
        approved.append(_ingest_finding(vault, domain, finding, tier=tier, db_path=db_path, domain_tag=domain_tag or str(contract.get("domain_primary") or "cross_arena")))
    return {"domain": domain, "query": query, "contract": str(contract_path(vault, domain)), "findings": len(findings), "ingested": approved, "skipped": skipped, "retrieved_at": now_utc()}


def _ingest_finding(vault: Path, domain: str, finding: SourceFinding, *, tier: str, db_path: Path | None, domain_tag: str) -> dict[str, Any]:
    title = finding.title or finding.locator
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="lisan-librarian-", encoding="utf-8") as handle:
        handle.write(f"# {title}\n\n{finding.excerpt}\n")
        handle.flush()
        result = ingest_reference_sources(
            [Path(handle.name)], vault=vault, db_path=db_path or sqlite_path(),
            on_exists="replace", domain_primary=domain_tag, source_tier=tier,
            source_origin=origin_for_url(finding.locator), source_url=finding.locator,
            retrieved_at=finding.retrieved_at or now_utc(), reindex=False,
        )
    from .rebuild_index import rebuild_index
    rebuild_index(vault=vault, db_path=db_path or sqlite_path())
    return {"url": finding.locator, "tier": tier, "chunks": result.get("total_chunks", 0)}


def consolidate_domain(vault: Path, domain: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """Curate exact duplicate knowledge conservatively.

    Semantic disagreements are reported, not guessed at. Exact duplicates
    are superseded in place, preserving their files and provenance history.
    """
    root = vault / "knowledge"
    groups: dict[str, list[tuple[Path, dict[str, Any], str]]] = {}
    for path in root.rglob("*.md") if root.exists() else []:
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        fm = doc.frontmatter
        if str(fm.get("type")) != "knowledge" or str(fm.get("domain_primary")) != domain:
            continue
        if str(fm.get("status")) != "active":
            continue
        digest = hashlib.sha256(" ".join(doc.body.split()).lower().encode()).hexdigest()
        groups.setdefault(digest, []).append((path, fm, doc.body))
    superseded: list[str] = []
    review: list[dict[str, Any]] = []
    for entries in groups.values():
        if len(entries) < 2:
            continue
        winner = max(entries, key=lambda item: __import__("lisan.tools.source_tiers", fromlist=["tier_rank"]).tier_rank(item[1].get("source_tier")))
        winner_id = str(winner[1].get("id"))
        for path, fm, _body in entries:
            record_id = str(fm.get("id"))
            if record_id == winner_id:
                continue
            if supersede_record(vault, record_id, db_path=db_path):
                superseded.append(record_id)
        review.append({"winner": winner_id, "members": [str(item[1].get("id")) for item in entries], "reason": "exact duplicate; highest source tier retained"})
    report = vault / "reports" / f"librarian-consolidation-{today_iso()}.md"
    write_markdown(report, {"id": f"report.librarian-consolidation-{today_iso()}", "type": "report", "created": today_iso(), "updated": today_iso(), "status": "active", "summary": f"Librarian consolidation for {domain}", "domain_primary": domain}, "# Librarian consolidation\n\n" + "\n".join(f"- {item}" for item in superseded) + "\n")
    return {"domain": domain, "superseded": superseded, "groups": review, "report": str(report)}


def correct_knowledge(vault: Path, record_id: str, correction: str) -> Path:
    """Record a natural-language owner correction without rewriting history."""
    for path in (vault / "knowledge").rglob("*.md") if (vault / "knowledge").exists() else []:
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("id")) != record_id:
            continue
        fm = dict(doc.frontmatter)
        fm["status"] = "disputed"
        fm["updated"] = today_iso()
        fm["review_notes"] = "Owner correction recorded; re-fetch or re-tier before superseding."
        body = doc.body.rstrip() + f"\n\n## Owner correction ({today_iso()})\n\n{correction.strip()}\n"
        write_markdown(path, fm, body)
        report = vault / "reports" / f"librarian-correction-{today_iso()}.md"
        write_markdown(report, {"id": f"report.librarian-correction-{today_iso()}", "type": "report", "created": today_iso(), "updated": today_iso(), "status": "active", "summary": f"Correction for {record_id}", "domain_primary": str(fm.get("domain_primary") or "cross_arena"), "domain_secondary": [], "privacy": "personal", "significance": "medium", "links": [record_id], "confidence": "high", "confidence_basis": "Owner correction", "last_confirmed": today_iso(), "review_after": today_iso()}, f"# Knowledge correction\n\nRecord `{record_id}` is disputed and requires source review.\n\nCorrection: {correction.strip()}\n")
        return report
    raise FileNotFoundError(f"knowledge record not found: {record_id}")
