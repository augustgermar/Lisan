from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.librarian import approve_origin, build_domain, contract_path, create_contract, decide_proposal, load_intake, propose_sources, resume_intake
from lisan.tools.research import SourceFinding
from lisan.tools.source_tiers import origin_matches, tier_confidence
from lisan.tools.execution_tools import TOOLS, build_tool_handlers


def test_source_tier_controls_confidence_and_origin_matching():
    assert tier_confidence("primary")[0] == "high"
    assert tier_confidence("unverified")[0] == "low"
    assert origin_matches("example.gov", "https://docs.example.gov/path")
    assert not origin_matches("example.gov", "https://example.com/path")


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


def test_build_fails_closed_while_intake_is_pending(tmp_path: Path):
    create_contract(tmp_path / "vault", "California SDP")
    from lisan.tools.librarian import _save_intake, intake_path
    _save_intake(intake_path(tmp_path / "vault", "California SDP"), {"domain": "California SDP", "status": "awaiting_owner", "proposals": [{"proposal_id": "origin-1", "status": "pending"}]})
    import pytest
    with pytest.raises(ValueError, match="pending"):
        build_domain(tmp_path / "vault", "California SDP", "training")
