from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.librarian import _normalize_source_query, _recommend_tier, approve_origin, build_domain, contract_path, create_contract, decide_proposal, load_intake, propose_sources, resolve_intake_domain, resume_intake
from lisan.tools.research import SourceFinding
from lisan.tools.source_tiers import origin_matches, tier_confidence
from lisan.tools.execution_tools import TOOLS, build_tool_handlers


def test_source_tier_controls_confidence_and_origin_matching():
    assert tier_confidence("primary")[0] == "high"
    assert tier_confidence("unverified")[0] == "low"
    assert origin_matches("example.gov", "https://docs.example.gov/path")
    assert not origin_matches("example.gov", "https://example.com/path")
    assert _normalize_source_query("Authoritative sources defining and documenting HTTP status codes. Provide exact URLs") == "HTTP status codes"
    assert _normalize_source_query("Authoritative specifications and official documentation defining HTTP status codes. Show exact URLs", domain="HTTP status codes") == "HTTP status codes"
    assert _recommend_tier(SourceFinding("web_search", "https://developer.mozilla.org/en-US/docs/Web/HTTP", "", publisher="developer.mozilla.org"))[0] == "official-secondary"


def test_source_query_carries_no_domain_vocabulary_between_subjects():
    """The 2026-08-21 Psyhacks incident.

    Source discovery appended "IETF RFC standards" to every query that was
    not already about standards, and split sentences at the period in
    "Dr.". The intake searched "... a chronological study of Dr IETF RFC
    standards" and proposed Minecraft build guides and a dictionary entry.
    This function serves every domain, so a term from one subject must
    never reach another.
    """
    query = (
        "Use https://www.youtube.com/@psychacks as the sole authoritative source "
        "for a chronological study of Dr. Orion Taraban's Psyhacks videos, "
        "beginning with the oldest available video."
    )
    normalized = _normalize_source_query(query, domain="Psyhacks / Dr. Orion Taraban")
    lowered = normalized.lower()
    assert "ietf" not in lowered and "rfc" not in lowered and "standards" not in lowered
    assert not lowered.endswith(" dr")
    assert "taraban" in lowered and "psychacks" in lowered
    # The URL survives as searchable words, not as an opaque locator.
    assert "youtube.com" in lowered and "https" not in lowered


def test_source_query_keeps_abbreviations_and_anchors_on_the_domain():
    assert _normalize_source_query("Find sources about Dr. Jane Goodall. Prefer her institute", domain="Jane Goodall").startswith("Jane Goodall")
    assert _normalize_source_query("Build a knowledge base about the St. Louis Cardinals", domain="St. Louis Cardinals") == "St. Louis Cardinals"
    assert _normalize_source_query("", domain="Anything") == ""


def test_standards_seeds_stay_out_of_unrelated_domains():
    """A "https://" URL contains the substring "http"; that must not arm
    the HTTP-standards seed path for a domain that has nothing to do with
    it."""
    from lisan.tools.librarian import _verified_standards_findings

    class Provider:
        def _fetch_page(self, url):
            raise AssertionError("unrelated domain must not fetch standards seeds")

    assert _verified_standards_findings("Psyhacks https://www.youtube.com/@psychacks videos", [Provider()]) == []


def test_contract_is_durable_and_origin_approval_is_append_only(tmp_path: Path):
    vault = tmp_path / "vault"
    path = create_contract(vault, "California SDP", domain_tag="california_sdp")
    approve_origin(vault, "California SDP", "dds.ca.gov", rationale="State authority")
    doc = load_markdown(path)
    assert doc.frontmatter["type"] == "domain_contract"
    assert doc.frontmatter["approved_origins"][0]["tier"] == "primary"
    assert contract_path(vault, "California SDP") == path


def test_librarian_is_exposed_to_conversation_tools(tmp_path: Path):
    tool = next(item for item in TOOLS if item["name"] == "librarian")
    assert "approve_proposal" in tool["parameters"]["properties"]["action"]["enum"]
    handlers = build_tool_handlers(vault=tmp_path / "vault", db_path=tmp_path / "lisan.sqlite", config={})
    result = __import__("json").loads(handlers["librarian"](action="resume_intake", domain="California SDP"))
    assert result["status"] == "not_started"


def test_intake_proposals_persist_and_resume_across_calls(tmp_path: Path, monkeypatch):
    finding = SourceFinding("web_search", "https://agency.gov/training", "official training requirements", "Training", publisher="agency.gov", retrieved_at="2026-08-16T00:00:00Z")
    monkeypatch.setattr("lisan.tools.librarian.search_published_sources", lambda *args, **kwargs: [finding])
    vault = tmp_path / "vault"
    proposed = propose_sources(vault, "California SDP", "training requirements", config={})
    assert proposed["proposals"][0]["status"] == "pending"
    assert load_intake(vault, "California SDP")["proposals"][0]["proposal_id"] == "origin-1"
    resumed = resume_intake(vault, "California SDP")
    assert resumed["needs_owner_input"] is True
    with __import__("pytest").raises(ValueError, match="exact proposal URL"):
        decide_proposal(vault, "California SDP", "origin-1", decision="approve", confirmed_url="https://agency.gov/wrong", tier="primary")
    decision = decide_proposal(vault, "California SDP", "origin-1", decision="approve", confirmed_url="https://agency.gov/training", tier="primary", rationale="Issuing agency")
    assert decision["intake_status"] == "approved"
    assert load_intake(vault, "California SDP")["proposals"][0]["status"] == "approved"


def test_proposal_identity_resolves_a_paraphrased_domain(tmp_path: Path, monkeypatch):
    finding = SourceFinding("web_search", "https://www.rfc-editor.org/rfc/rfc9110.html", "HTTP Semantics", "RFC 9110", publisher="www.rfc-editor.org")
    monkeypatch.setattr("lisan.tools.librarian.search_published_sources", lambda *args, **kwargs: [finding])
    vault = tmp_path / "vault"
    propose_sources(vault, "HTTP status codes", "HTTP status codes RFC", config={})
    assert resolve_intake_domain(vault, "HTTP Semantics", proposal_id="origin-1", confirmed_url=finding.locator) == "HTTP status codes"


def test_http_standards_fallback_proposes_verified_primary_candidates(tmp_path: Path, monkeypatch):
    from lisan.tools.librarian import _verified_standards_findings

    class Provider:
        def _fetch_page(self, url):
            return ("Canonical standard", "HTTP status code specification", [])

    monkeypatch.setattr("lisan.tools.librarian.WebSearchProvider", Provider)
    findings = _verified_standards_findings("HTTP status codes IETF RFC standards", [Provider()])
    assert [item.locator for item in findings] == [
        "https://www.rfc-editor.org/rfc/rfc9110.html",
        "https://www.iana.org/assignments/http-status-codes/http-status-codes.xhtml",
    ]


def test_build_fails_closed_while_intake_is_pending(tmp_path: Path):
    create_contract(tmp_path / "vault", "California SDP")
    from lisan.tools.librarian import _save_intake, intake_path
    _save_intake(intake_path(tmp_path / "vault", "California SDP"), {"domain": "California SDP", "status": "awaiting_owner", "proposals": [{"proposal_id": "origin-1", "status": "pending"}]})
    import pytest
    with pytest.raises(ValueError, match="pending"):
        build_domain(tmp_path / "vault", "California SDP", "training")


def test_approved_proposal_url_is_fetched_directly_during_build(tmp_path: Path, monkeypatch):
    from lisan.tools.librarian import _approved_source_findings, _save_intake, intake_path

    class Provider:
        def _fetch_page(self, url):
            return ("RFC 9110", "HTTP Semantics", [])

    monkeypatch.setattr("lisan.tools.librarian.WebSearchProvider", Provider)

    vault = tmp_path / "vault"
    create_contract(vault, "HTTP status codes")
    _save_intake(intake_path(vault, "HTTP status codes"), {
        "domain": "HTTP status codes", "status": "approved",
        "proposals": [{"proposal_id": "origin-1", "origin": "www.rfc-editor.org", "url": "https://www.rfc-editor.org/rfc/rfc9110.html", "status": "approved"}],
    })
    findings = _approved_source_findings(vault, {"domain_name": "HTTP status codes", "approved_origins": [{"origin": "www.rfc-editor.org", "tier": "primary"}]}, [Provider()])
    assert findings[0].locator.endswith("rfc9110.html")
    assert findings[0].document_text == "HTTP Semantics"


def test_intake_reports_a_dead_search_backend_instead_of_an_empty_result(tmp_path: Path, monkeypatch):
    """"No proposals" must not be the same answer as "search is down"."""
    from lisan.tools.research import SearchProviderError

    class Broken:
        name = "web_search"

        def search(self, query, *, limit):
            raise SearchProviderError("key rejected")

    monkeypatch.setattr("lisan.tools.librarian.installed_published_providers", lambda **_kw: [Broken()])
    result = propose_sources(tmp_path / "vault", "Psyhacks", "Psyhacks Orion Taraban videos", config={})
    assert result["proposals"] == []
    assert result["search_unavailable"] is True
    assert result["search_errors"] == ["web_search: key rejected"]
    assert load_intake(tmp_path / "vault", "Psyhacks")["search_errors"] == ["web_search: key rejected"]
