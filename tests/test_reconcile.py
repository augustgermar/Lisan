"""WO-6: the self-belief reconciliation job — the growth-arc mechanic.

A synthetic contradiction (a modest belief vs. successful episodes)
produces a chained revision with real evidence pointers; proposals citing
nonexistent episodes or beliefs are rejected by the deterministic gate;
and a fallback (no-provider) response revises nothing.
"""
from __future__ import annotations

from pathlib import Path

from lisan.frontmatter import load_markdown
from lisan.tools.dreamer_ops import _apply_belief_revisions, _bundle_self_reconciliation
from lisan.tools.self_beliefs import new_self_belief
from lisan.tools.self_episodes import SelfEvent, write_self_episode


def _seed(vault: Path) -> Path:
    belief = new_self_belief(
        vault, "I am not reliable at completing multi-step plans.", confidence="medium"
    )
    for i, (kind, outcome) in enumerate([("plan", "succeeded"), ("plan", "succeeded"), ("task", "succeeded")]):
        write_self_episode(
            vault,
            SelfEvent(
                event_id=f"job-{i}",
                event_kind=kind,
                date=f"2026-07-0{i + 1}",
                title=f"{kind} {i}",
                narration="{{self}} completed a multi-step plan for {{principal}}.",
                outcome=outcome,
                source_refs=[f"jobs:{i}"],
            ),
        )
    return belief


def test_bundle_contains_beliefs_and_evidence_pool(tmp_path):
    _seed(tmp_path)
    bundle = _bundle_self_reconciliation(tmp_path)
    assert "not reliable at completing multi-step plans" in bundle
    assert "self_episode.job-0" in bundle
    assert "outcome: succeeded" in bundle


def test_contradiction_produces_chained_revision(tmp_path):
    belief_path = _seed(tmp_path)
    response = {
        "revisions": [
            {
                "belief_id": "self_belief.i-am-not-reliable-at-completing-multi-step-plans",
                "new_statement": "I complete multi-step plans reliably when they are well-scoped.",
                "new_confidence": "medium",
                "reason": "two plan runs and a task completed without failure",
                "evidence_refs": ["self_episode.job-0", "self_episode.job-1"],
            }
        ]
    }
    applied = _apply_belief_revisions(tmp_path, response)
    assert applied == belief_path
    doc = load_markdown(belief_path)
    assert "reliably when they are well-scoped" in doc.frontmatter["summary"]
    revisions = doc.frontmatter["revisions"]
    assert len(revisions) == 1
    assert revisions[0]["previous_statement"].startswith("I am not reliable")
    assert set(revisions[0]["evidence_refs"]) == {"self_episode.job-0", "self_episode.job-1"}
    assert "I believed" in doc.body


def test_fabricated_evidence_is_rejected(tmp_path):
    belief_path = _seed(tmp_path)
    response = {
        "revisions": [
            {
                "belief_id": "self_belief.i-am-not-reliable-at-completing-multi-step-plans",
                "new_statement": "I am flawless.",
                "new_confidence": "high",
                "reason": "imagined triumphs",
                "evidence_refs": ["self_episode.job-999", "self_episode.invented"],
            }
        ]
    }
    assert _apply_belief_revisions(tmp_path, response) is None
    doc = load_markdown(belief_path)
    assert doc.frontmatter["summary"].startswith("I am not reliable")
    assert doc.frontmatter["revisions"] == []


def test_unknown_belief_and_empty_response_are_noops(tmp_path):
    _seed(tmp_path)
    assert _apply_belief_revisions(tmp_path, {"revisions": [{"belief_id": "self_belief.nope",
                                                             "new_statement": "x",
                                                             "new_confidence": "low",
                                                             "evidence_refs": ["self_episode.job-0"]}]}) is None
    assert _apply_belief_revisions(tmp_path, {}) is None
    assert _apply_belief_revisions(tmp_path, {"revisions": []}) is None


def test_partial_evidence_survives_the_gate(tmp_path):
    """Real refs are kept, fabricated ones dropped; the revision applies on
    what survives."""
    belief_path = _seed(tmp_path)
    response = {
        "revisions": [
            {
                "belief_id": "self_belief.i-am-not-reliable-at-completing-multi-step-plans",
                "new_statement": "I complete plans more reliably than I believed.",
                "new_confidence": "medium",
                "reason": "the record shows completions",
                "evidence_refs": ["self_episode.job-2", "self_episode.hallucinated"],
            }
        ]
    }
    applied = _apply_belief_revisions(tmp_path, response)
    assert applied == belief_path
    revisions = load_markdown(belief_path).frontmatter["revisions"]
    assert revisions[0]["evidence_refs"] == ["self_episode.job-2"]
