"""Contract-driven domain knowledge librarian.

The contract is the durable permission record.  A build may search broadly,
but it only promotes material from origins explicitly approved for that
domain; everything else remains unverified and is not silently upgraded.
"""
from __future__ import annotations

import tempfile
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..config import load_config
from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from ..utils import slugify, today_iso
from .ingest import ingest_reference_sources
from .research import SourceFinding, WebSearchProvider, installed_published_providers, search_published_sources
from .source_tiers import SOURCE_TIERS, normalize_source_tier, now_utc, origin_for_url, origin_matches
from .record_factory import supersede_record


def contract_path(vault: Path, domain: str) -> Path:
    return vault / "domains" / slugify(domain) / "sourcing-contract.md"


def intake_path(vault: Path, domain: str) -> Path:
    """Durable, domain-keyed async intake state; safe across restarts."""
    return vault / "domains" / slugify(domain) / "librarian-intake.json"


def _save_intake(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_utc()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_intake(vault: Path, domain: str) -> dict[str, Any] | None:
    path = intake_path(vault, domain)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid librarian intake state: {path}") from exc
    return value if isinstance(value, dict) else None


def _recommend_tier(finding: SourceFinding) -> tuple[str, str]:
    """A proposal, never an approval: only a hint for the owner's decision."""
    origin = origin_for_url(finding.locator)
    if origin in {"rfc-editor.org", "www.rfc-editor.org", "iana.org", "www.iana.org", "ietf.org", "www.ietf.org"}:
        return "primary", "Recognized standards publisher or registry; owner must confirm that it is authoritative for this domain."
    if origin.endswith(".gov") or origin.endswith(".gov.uk"):
        return "primary", "Government origin appears potentially authoritative; owner must confirm issuing authority."
    if origin == "developer.mozilla.org":
        return "official-secondary", "Mozilla technical documentation: reputable and derivative of standards, but not the issuing standards body."
    if origin in {"docs.python.org", "learn.microsoft.com", "cloud.google.com", "developer.apple.com"} or any(token in origin for token in ("official", "state", "agency", "university", "edu")):
        return "official-secondary", "Origin appears institutional; owner must confirm its authority and scope."
    return "community", "No authoritative-origin signal detected; treat as community until the owner decides otherwise."


def _page_fetcher(providers: list[Any]) -> Any:
    """Something that can fetch a named page.

    Fetching a URL is not searching for one: it kept working through the
    2026-08-21 discovery outage. It must not depend on which search
    backend is configured, so fall back to a bare fetcher when the
    configured provider is not one.
    """
    provider = next((item for item in providers if isinstance(item, WebSearchProvider)), None)
    return provider if provider is not None else WebSearchProvider()


def _verified_standards_findings(query: str, providers: list[Any]) -> list[SourceFinding]:
    """Fetch canonical standards pages when a standards-oriented HTTP query is requested.

    Search engines are useful discovery tools but may return vocabulary pages
    for terms such as "authoritative". These verified, narrow seeds improve
    recall without granting authority: the owner still approves each origin.
    """
    # Match whole words, and never inside a URL: every "https://..." the
    # owner supplies contains the substring "http", which made this
    # standards-only path fire on unrelated domains.
    lowered = re.sub(r"https?://\S+", " ", query.lower())
    if not re.search(r"\bhttp\b", lowered) or not re.search(r"\bstatus\b", lowered):
        return []
    seeds = [
        ("https://www.rfc-editor.org/rfc/rfc9110.html", "RFC 9110: HTTP Semantics"),
        ("https://www.iana.org/assignments/http-status-codes/http-status-codes.xhtml", "HTTP Status Code Registry"),
    ]
    findings: list[SourceFinding] = []
    provider = _page_fetcher(providers)
    retrieved = now_utc()
    for url, fallback_title in seeds:
        page = provider._fetch_page(url)
        if page is None:
            continue
        title, text, _links = page
        findings.append(SourceFinding(
            source="web_search", locator=url, excerpt=(text or fallback_title)[:1200],
            title=title or fallback_title, observed_at=retrieved,
            publisher=origin_for_url(url), retrieved_at=retrieved,
            confidence=0.95, unverifiable=False,
            document_text=text,
        ))
    return findings


# Sentence-ending punctuation is ambiguous: the period in "Dr." or "U.S."
# ends an abbreviation, not a clause. Splitting on those truncated the
# 2026-08-21 Psyhacks intake to "... a chronological study of Dr".
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "st", "sr", "jr", "vs", "etc", "inc",
    "ltd", "co", "corp", "no", "vol", "ed", "eds", "approx", "fig", "al",
}

# Scaffolding the model wraps around a subject when it calls this tool.
# Every pattern here must describe the *task* ("propose sources for"),
# never a subject: domain vocabulary in this function is a defect, not a
# tuning knob — see the 2026-08-21 incident note in propose_sources.
_TASK_PREFIXES = (
    r"^(?:please\s+)?(?:propose|find|discover|identify|search\s+for|gather|collect|compile|gather\s+up)\s+",
    r"^(?:please\s+)?(?:use|build|create|start)\s+(?:a\s+|an\s+|the\s+)?(?:knowledge\s+base\s+(?:about|for|on|of)\s+)?",
    r"^(?:authoritative\s+|official\s+|primary\s+)*sources?\s+(?:for|about|defining\s+and\s+documenting|defining|documenting|on|of)\s+",
    r"^(?:and\s+)?documenting\s+",
)

# Instruction clauses that can appear mid-string once a URL is the subject.
_TASK_CLAUSES = (
    r"\bas\s+the\s+(?:sole|only|single|primary|definitive)\s+(?:authoritative\s+)?sources?\s*(?:for|of|on)?\b",
    r"\bas\s+(?:the\s+)?(?:authoritative\s+)?sources?\s*(?:for|of|on)?\b",
)

# Function words may legitimately repeat; content words may not.
_QUERY_STOPWORDS = {"a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with"}

_MAX_QUERY_WORDS = 16


def _first_sentence(text: str) -> str:
    """The first clause that a period actually ends.

    A period followed by whitespace only ends a sentence when the token
    before it is neither an abbreviation nor a single initial.
    """
    for match in re.finditer(r"[.;]\s+", text):
        head = text[: match.start()]
        token = re.search(r"([A-Za-z.]+)$", head)
        word = (token.group(1) if token else "").lower().strip(".")
        if len(word) <= 1 or word in _ABBREVIATIONS:
            continue
        return head
    return text


def _url_terms(match: "re.Match[str]") -> str:
    """Turn a URL into the words a search engine can match.

    The raw URL is preserved by the caller as the owner's intended origin;
    here we want its readable identity ("youtube.com psychacks"), because
    search engines rank poorly on a bare URL string.
    """
    host = match.group(1)
    path = match.group(2) or ""
    tail = [part for part in re.split(r"[/@._-]+", path) if len(part) > 1]
    return " ".join([host, *tail[:2]])


def _normalize_source_query(query: str, domain: str | None = None) -> str:
    """Reduce a conversational instruction to the subject worth searching.

    Subject-agnostic by contract. This runs ahead of every domain intake,
    so any vocabulary specific to one subject added here poisons all the
    others — which is exactly what happened between 2026-08-16 and
    2026-08-21.
    """
    clean = " ".join(str(query or "").strip().split())
    if not clean:
        return ""
    clean = _first_sentence(clean)
    clean = re.split(r",\s*(?:prioritizing|provide|show|do not|without|beginning|starting)\b", clean, maxsplit=1, flags=re.I)[0]
    for pattern in _TASK_PREFIXES:
        clean = re.sub(pattern, "", clean, flags=re.I)
    clean = re.sub(r"https?://(?:www\.)?([^\s/]+)((?:/[^\s]*)?)", _url_terms, clean)
    for pattern in _TASK_CLAUSES:
        clean = re.sub(pattern, " ", clean, flags=re.I)
    if domain and str(domain).lower() in clean.lower():
        # Anchor on the requested domain, not on adjectives describing the
        # desired authority. This handles model wording such as "official
        # documentation defining HTTP status codes" without a growing list
        # of prose-prefix patterns.
        start = clean.lower().find(str(domain).lower())
        clean = clean[start:]
    elif domain:
        # The subject the owner named is the most reliable anchor there is.
        clean = f"{domain} {clean}"
    clean = " ".join(clean.split())
    words: list[str] = []
    seen: set[str] = set()
    for word in clean.split(" "):
        key = re.sub(r"[^\w]", "", word).lower()
        if not key:
            # A stray separator from a domain label like "A / B" carries no
            # search signal.
            continue
        if key not in _QUERY_STOPWORDS and key in seen:
            # Anchoring on the domain can repeat words the query already
            # had; a doubled term adds nothing to a search engine.
            continue
        seen.add(key)
        words.append(word)
    return " ".join(words[:_MAX_QUERY_WORDS]).strip(" .,;-")


def _owner_named_origins(query: str, providers: list[Any]) -> list[SourceFinding]:
    """URLs the owner typed are candidates already — do not go looking.

    On 2026-08-21 the owner wrote "use https://www.youtube.com/@psychacks
    as the sole authoritative source" and the intake ran a web search
    anyway, proposing a Minecraft build site. An origin the owner names is
    the strongest signal available here; it still becomes a *pending*
    proposal that the owner has to approve, because naming a URL in
    passing is not the same act as ratifying it as authoritative.
    """
    urls: list[str] = []
    for raw in re.findall(r"https?://[^\s<>\"')\]]+", str(query or "")):
        candidate = raw.rstrip(".,;:!?)")
        if candidate not in urls:
            urls.append(candidate)
    if not urls:
        return []
    provider = _page_fetcher(providers)
    retrieved = now_utc()
    findings: list[SourceFinding] = []
    for url in urls[:5]:
        title, text = "", ""
        # Fetching a named page is unrelated to search discovery, and kept
        # working when discovery did not.
        page = provider._fetch_page(url)
        if page is not None:
            title, text, _links = page
        findings.append(SourceFinding(
            source="owner_named", locator=url, excerpt=(text or url)[:1200],
            title=title or url, observed_at=retrieved, publisher=origin_for_url(url),
            retrieved_at=retrieved, confidence=0.6, unverifiable=False,
            document_text=text,
        ))
    return findings


def propose_sources(
    vault: Path,
    domain: str,
    query: str,
    *,
    config: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search for candidates and persist them as pending owner decisions."""
    if not str(query or "").strip():
        raise ValueError("source proposals require a focused query")
    create_contract(vault, domain)
    state = load_intake(vault, domain) or {
        "domain": domain, "status": "awaiting_owner", "query": query,
        "created_at": now_utc(), "proposals": [],
    }
    existing = {str(item.get("origin")) for item in state.get("proposals") or [] if isinstance(item, dict)}
    cfg = config or load_config()
    search_query = _normalize_source_query(query, domain=domain)
    providers = installed_published_providers(config=cfg)
    search_queries = [search_query]
    if re.search(r"\b(?:rfc|ietf|standard)\w*\b", str(query), flags=re.I):
        search_queries.extend([
            f'"{search_query}" RFC 9110',
            f"site:rfc-editor.org {search_query}",
            f"site:iana.org {search_query} registry",
        ])
    findings: list[SourceFinding] = []
    search_errors: list[str] = []
    named = _owner_named_origins(query, providers)
    findings.extend(named)
    findings.extend(_verified_standards_findings(search_query, providers))
    for candidate_query in [] if named else search_queries:
        findings.extend(search_published_sources(
            candidate_query, providers=providers, max_results_per_source=max(1, min(int(limit), 20)),
            errors=search_errors,
        ))
    next_number = len(state.get("proposals") or []) + 1
    for finding in findings:
        origin = origin_for_url(finding.locator)
        if not origin or origin in existing:
            continue
        recommendation, basis = _recommend_tier(finding)
        state.setdefault("proposals", []).append({
            "proposal_id": f"origin-{next_number}", "status": "pending", "origin": origin,
            "url": finding.locator, "title": finding.title, "publisher": finding.publisher or origin,
            "excerpt": finding.excerpt, "recommended_tier": recommendation, "tier_basis": basis,
            "proposed_at": finding.retrieved_at or now_utc(),
        })
        existing.add(origin)
        next_number += 1
    state["query"] = query
    state["search_query"] = search_query
    state["status"] = "awaiting_owner"
    # An empty proposal list has two very different causes: the search
    # worked and matched nothing, or the backend never answered. Recording
    # which lets the agent report the real reason instead of "it
    # malfunctioned" — the 2026-08-21 Psyhacks intake could not tell the
    # owner why it had nothing.
    if search_errors:
        state["search_errors"] = sorted(set(search_errors))
    else:
        state.pop("search_errors", None)
    state["discovery"] = "owner_named" if named else "search"
    _save_intake(intake_path(vault, domain), state)
    result = {
        "domain": domain, "intake": str(intake_path(vault, domain)), "status": state["status"],
        "search_query": search_query, "proposals": state.get("proposals", []),
        "needs_owner_input": True,
    }
    if search_errors:
        result["search_errors"] = state["search_errors"]
        result["search_unavailable"] = not state.get("proposals")
    return result


def resume_intake(vault: Path, domain: str) -> dict[str, Any]:
    state = load_intake(vault, domain)
    if state is None:
        return {"domain": domain, "status": "not_started", "needs_owner_input": True, "next": "Ask for a focused domain question to begin source proposals."}
    return {"domain": domain, "intake": str(intake_path(vault, domain)), **state, "needs_owner_input": any(str(item.get("status")) == "pending" for item in state.get("proposals", []))}


def resolve_intake_domain(
    vault: Path,
    domain: str,
    *,
    proposal_id: str | None = None,
    confirmed_url: str | None = None,
) -> str:
    """Resolve model paraphrases using durable proposal identity, never text alone."""
    if load_intake(vault, domain) is not None:
        return domain
    matches: list[str] = []
    root = vault / "domains"
    for path in root.glob("*/librarian-intake.json") if root.exists() else []:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for proposal in state.get("proposals") or []:
            if not isinstance(proposal, dict):
                continue
            if proposal_id and str(proposal.get("proposal_id")) != str(proposal_id):
                continue
            if confirmed_url and str(proposal.get("url")) != str(confirmed_url):
                continue
            matches.append(str(state.get("domain") or path.parent.name))
            break
    if len(matches) == 1:
        return matches[0]
    return domain


def _refresh_intake_status(vault: Path, domain: str, state: dict[str, Any]) -> None:
    pending = any(str(item.get("status")) == "pending" for item in state.get("proposals", []))
    approved = any(str(item.get("status")) == "approved" for item in state.get("proposals", []))
    state["status"] = "awaiting_owner" if pending else ("approved" if approved else "rejected")
    _save_intake(intake_path(vault, domain), state)
    path = contract_path(vault, domain)
    if path.exists():
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        fm["intake_status"] = state["status"]
        fm["updated"] = today_iso()
        write_markdown(path, fm, doc.body)


def decide_proposal(
    vault: Path,
    domain: str,
    proposal_id: str,
    *,
    decision: str,
    confirmed_url: str | None = None,
    tier: str | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Apply one explicit owner decision; missing exact provenance fails closed."""
    state = load_intake(vault, domain)
    if state is None:
        raise ValueError(f"no pending intake exists for {domain}")
    proposal = next((item for item in state.get("proposals", []) if str(item.get("proposal_id")) == proposal_id), None)
    if not isinstance(proposal, dict):
        raise ValueError(f"unknown source proposal: {proposal_id}")
    if str(proposal.get("status")) != "pending":
        raise ValueError(f"source proposal already decided: {proposal_id}")
    decision = str(decision).strip().lower()
    if decision == "approve":
        if str(confirmed_url or "").strip() != str(proposal.get("url") or "").strip():
            raise ValueError("approval requires the exact proposal URL to be confirmed")
        selected = normalize_source_tier(tier)
        if selected not in {"primary", "official-secondary"}:
            raise ValueError("authoritative approval must explicitly choose primary or official-secondary")
        approve_origin(vault, domain, str(proposal["origin"]), tier=selected, source_url=str(proposal["url"]), rationale=rationale or str(proposal.get("tier_basis") or ""))
        proposal.update({"status": "approved", "owner_tier": selected, "decided_at": now_utc(), "decision": "approved"})
    elif decision in {"reject", "down_tier"}:
        selected = normalize_source_tier(tier) if decision == "down_tier" else "unverified"
        if decision == "down_tier" and selected not in {"community", "owner-authored", "unverified"}:
            raise ValueError("down-tier decisions must choose community, owner-authored, or unverified")
        proposal.update({"status": "rejected" if decision == "reject" else "down_tiered", "owner_tier": selected, "decided_at": now_utc(), "decision": decision})
        path = contract_path(vault, domain)
        doc = load_markdown(path)
        fm = dict(doc.frontmatter)
        rejected = list(fm.get("rejected_origins") or [])
        rejected.append({"origin": proposal["origin"], "url": proposal["url"], "decision": decision, "tier": selected, "rationale": rationale, "decided_at": today_iso()})
        fm["rejected_origins"] = rejected
        write_markdown(path, fm, doc.body.rstrip() + f"\n\nOrigin decision: `{proposal['origin']}` — `{decision}` ({selected}).\n")
    else:
        raise ValueError("decision must be approve, reject, or down_tier")
    _refresh_intake_status(vault, domain, state)
    return {"domain": domain, "proposal": proposal, "intake_status": state["status"], "intake": str(intake_path(vault, domain))}


def finalize_intake(vault: Path, domain: str) -> dict[str, Any]:
    state = load_intake(vault, domain)
    if state is None:
        raise ValueError(f"no intake exists for {domain}")
    pending = [str(item.get("proposal_id")) for item in state.get("proposals", []) if str(item.get("status")) == "pending"]
    if pending:
        raise ValueError(f"intake still has pending owner decisions: {', '.join(pending)}")
    if not any(str(item.get("status")) == "approved" for item in state.get("proposals", [])):
        raise ValueError("intake cannot be finalized without at least one approved origin")
    _refresh_intake_status(vault, domain, state)
    return resume_intake(vault, domain)


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
        "intake_status": "draft",
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
    source_url: str | None = None,
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
        origins.append({"origin": clean, "tier": tier, "source_url": source_url or "", "rationale": rationale, "approved_by": approved_by, "approved_at": today_iso()})
    fm["approved_origins"] = origins
    # Direct CLI approval remains an explicit manual override. Conversational
    # approval uses decide_proposal, which also persists the proposal decision.
    if not load_intake(vault, domain):
        fm["intake_status"] = "approved"
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
    intake = load_intake(vault, domain)
    if str(contract.get("intake_status") or "") != "approved":
        pending = [str(item.get("proposal_id")) for item in (intake or {}).get("proposals", []) if str(item.get("status")) == "pending"]
        if pending:
            raise ValueError(f"build blocked: owner decisions are still pending for {', '.join(pending)}")
        raise ValueError("build blocked: sourcing intake is not finalized; propose and resolve origins first")
    config = load_config()
    providers = installed_published_providers(config=config)
    findings = _approved_source_findings(vault, contract, providers)
    findings.extend(search_published_sources(query, providers=providers, max_results_per_source=limit))
    unique_findings: list[SourceFinding] = []
    seen_urls: set[str] = set()
    for finding in findings:
        if finding.locator in seen_urls:
            continue
        seen_urls.add(finding.locator)
        unique_findings.append(finding)
    findings = unique_findings
    approved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        tier = _approved_tier(contract, finding.locator)
        if tier is None:
            skipped.append({"url": finding.locator, "reason": "origin not approved by contract"})
            continue
        approved.append(_ingest_finding(vault, domain, finding, tier=tier, db_path=db_path, domain_tag=domain_tag or str(contract.get("domain_primary") or "cross_arena")))
    return {"domain": domain, "query": query, "contract": str(contract_path(vault, domain)), "findings": len(findings), "ingested": approved, "skipped": skipped, "retrieved_at": now_utc()}


def _approved_source_findings(vault: Path, contract: dict[str, Any], providers: list[Any]) -> list[SourceFinding]:
    """Fetch exact URLs the owner approved; search is only supplemental discovery."""
    intake = load_intake(vault, str(contract.get("domain_name") or ""))
    proposal_urls = {
        str(item.get("origin")): str(item.get("url"))
        for item in (intake or {}).get("proposals", [])
        if isinstance(item, dict) and str(item.get("status")) == "approved" and item.get("url")
    }
    provider = next((item for item in providers if isinstance(item, WebSearchProvider)), None)
    if provider is None:
        return []
    findings: list[SourceFinding] = []
    retrieved = now_utc()
    for entry in contract.get("approved_origins") or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("source_url") or proposal_urls.get(str(entry.get("origin") or "")))
        if not url:
            continue
        page = provider._fetch_page(url)
        if page is None:
            continue
        title, text, _links = page
        findings.append(SourceFinding(
            source="web_search", locator=url, excerpt=(text or title)[:1200], title=title,
            observed_at=retrieved, publisher=origin_for_url(url), retrieved_at=retrieved,
            confidence=0.95, unverifiable=False,
            document_text=text,
        ))
    return findings


def _ingest_finding(vault: Path, domain: str, finding: SourceFinding, *, tier: str, db_path: Path | None, domain_tag: str) -> dict[str, Any]:
    title = finding.title or finding.locator
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="lisan-librarian-", encoding="utf-8") as handle:
        handle.write(f"# {title}\n\n{finding.document_text or finding.excerpt}\n")
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
