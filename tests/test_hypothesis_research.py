from __future__ import annotations

from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.hypothesis_research import investigate_hypothesis
from lisan.tools.record_factory import new_pattern
from lisan.tools.research import SourceFinding


class FakeWebProvider:
    name = "fake-web"

    def __init__(self, findings):
        self.findings = findings

    def search(self, query: str, *, limit: int):
        return self.findings[:limit]


def _pattern(vault: Path) -> Path:
    return new_pattern(
        vault,
        pattern_type="other",
        hypothesis="A narrow hypothesis worth checking against public information.",
        supporting_records=[],
        evidence_needed=["More evidence"],
    ).path


def test_single_web_finding_creates_provenance_report_without_rewriting_hypothesis(tmp_path: Path):
    ensure_repo_layout(tmp_path)
    vault = vault_root(tmp_path)
    pattern = _pattern(vault)
    before = load_markdown(pattern).frontmatter["hypothesis"]
    out = investigate_hypothesis(
        vault=vault,
        hypothesis=str(pattern),
        question="What does the public source say about this narrow question?",
        config={"drive": {"action_tier": 3}, "enrichment": {"max_candidates_per_source": 3}},
        published_providers=[FakeWebProvider([SourceFinding("web", "https://example.test/source", "A sourced answer", title="Source")])],
    )
    assert out["status"] == "candidate_found"
    assert Path(out["report"]).exists()
    assert load_markdown(pattern).frontmatter["hypothesis"] == before
    report = load_markdown(Path(out["report"])).frontmatter
    assert report["web_findings"][0]["url"] == "https://example.test/source"


def test_multiple_web_findings_create_owner_question_loop(tmp_path: Path):
    ensure_repo_layout(tmp_path)
    vault = vault_root(tmp_path)
    pattern = _pattern(vault)
    findings = [
        SourceFinding("web", "https://example.test/a", "first", title="A"),
        SourceFinding("web", "https://example.test/b", "second", title="B"),
    ]
    out = investigate_hypothesis(
        vault=vault,
        hypothesis=str(pattern),
        question="Which public account is relevant?",
        config={"drive": {"action_tier": 3}},
        published_providers=[FakeWebProvider(findings)],
    )
    assert out["status"] == "needs_owner"
    loop = load_markdown(Path(out["owner_loop"])).frontmatter
    assert loop["origin"] == "research"
    assert loop["next_action"] == "ask_owner"
    assert "owner_question" in loop
