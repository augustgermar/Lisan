"""The current brief: active state summaries with TTL-based staleness."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from lisan.frontmatter import load_markdown, write_markdown
from lisan.tools.current_brief import (
    BriefState,
    _is_stale,
    _load_states,
    generate_current_brief,
    write_current_brief,
)
from lisan.utils import today_iso


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _state(vault: Path, name: str, **fm: object) -> None:
    path = vault / "state" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(path, {"type": "state", **fm}, "body")


def test_is_stale() -> None:
    assert _is_stale(_days_ago(31), 30) is True
    assert _is_stale(_days_ago(30), 30) is False
    assert _is_stale(None, 30) is False
    assert _is_stale(_days_ago(400), None) is False
    assert _is_stale("not-a-date", 30) is False


def test_brief_state_aliases_point_at_the_domain() -> None:
    state = BriefState("health", "s", "low", False, None, None, None)
    assert state.category == "health"
    assert state.arena == "health"


def test_empty_vault_says_so(tmp_path: Path) -> None:
    brief = generate_current_brief(tmp_path)
    assert brief.startswith("# Current Brief\n")
    assert f"Generated: {today_iso()}" in brief
    assert "- freshness: observed_at=" in brief
    assert "No active state files exist yet." in brief
    assert "## Active Domains" not in brief


def test_load_states_normalizes_and_skips_non_state_files(tmp_path: Path) -> None:
    _state(tmp_path, "a-work", domain_primary="work", summary="Shipping.", confidence="high",
           ttl_days=30, updated=_days_ago(5), review_after="2027-01-01")
    _state(tmp_path, "b-legacy", arena_primary="health", summary="Resting.", ttl_days="14", updated=_days_ago(20))
    _state(tmp_path, "c-bare", ttl_days="soon")
    _state(tmp_path, "d-empty-ttl", domain_primary="money", ttl_days="", updated=_days_ago(900))
    write_markdown(tmp_path / "state" / "e-not-state.md", {"type": "entity", "summary": "ignored"}, "body")
    (tmp_path / "state" / "f-broken.md").write_text("---\n{not json\n---\n", encoding="utf-8")

    states = _load_states(tmp_path)

    assert [s.domain for s in states] == ["work", "health", "cross_arena", "money"]
    work, legacy, bare, empty_ttl = states
    assert (work.summary, work.confidence, work.ttl_days, work.stale) == ("Shipping.", "high", 30, False)
    assert work.review_after == "2027-01-01"
    assert (legacy.ttl_days, legacy.stale, legacy.confidence) == (14, True, "low")
    assert (bare.summary, bare.ttl_days, bare.updated, bare.review_after, bare.stale) == ("", None, None, None, False)
    assert (empty_ttl.ttl_days, empty_ttl.stale) == (None, False)


def test_brief_lists_active_and_stale_domains(tmp_path: Path) -> None:
    _state(tmp_path, "work", domain_primary="work", summary="Shipping the release.", confidence="high",
           ttl_days=30, updated=_days_ago(5), review_after="2027-01-01")
    _state(tmp_path, "health", domain_primary="health", summary="Recovering from a cold.",
           ttl_days=7, updated=_days_ago(20))
    _state(tmp_path, "home", domain_primary="home", summary="Quiet.")

    brief = generate_current_brief(tmp_path)

    assert brief.index("### health") < brief.index("### home") < brief.index("### work")
    work_block = brief.split("### work", 1)[1]
    for line in (
        "- summary: Shipping the release.",
        "- confidence: high",
        "- freshness: current",
        "- ttl_days: 30d",
        f"- updated: {_days_ago(5)}",
        "- review_after: 2027-01-01",
    ):
        assert line in work_block
    home_block = brief.split("### home", 1)[1].split("###", 1)[0]
    for line in ("- confidence: low", "- ttl_days: none", "- updated: unknown", "- review_after: none"):
        assert line in home_block
    health_block = brief.split("### health", 1)[1].split("###", 1)[0]
    assert "- freshness: stale" in health_block

    stale_section = brief.split("## Stale Domains", 1)[1]
    assert stale_section.strip() == "- health: Recovering from a cold."


def test_no_stale_section_when_everything_is_current(tmp_path: Path) -> None:
    _state(tmp_path, "work", domain_primary="work", summary="Fine.", ttl_days=30, updated=_days_ago(1))
    assert "## Stale Domains" not in generate_current_brief(tmp_path)


def test_write_current_brief_stamps_a_one_day_validity_window(tmp_path: Path) -> None:
    _state(tmp_path, "work", domain_primary="work", summary="Fine.")

    out = write_current_brief(tmp_path)

    assert out == tmp_path / "primer" / "current-brief.md"
    doc = load_markdown(out)
    assert doc.frontmatter["type"] == "current_brief"
    assert doc.frontmatter["ttl_days"] == 1
    observed = datetime.fromisoformat(doc.frontmatter["observed_at"])
    valid_until = datetime.fromisoformat(doc.frontmatter["valid_until"])
    assert valid_until - observed == timedelta(days=1)
    assert "### work" in doc.body
    assert "- summary: Fine." in doc.body
