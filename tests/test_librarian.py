from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.librarian import approve_origin, contract_path, create_contract
from lisan.tools.source_tiers import origin_matches, tier_confidence


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
