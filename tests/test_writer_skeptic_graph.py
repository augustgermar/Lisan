"""Focused coverage for the bounded Writer-Skeptic graph.

Covers: immediate approval, revision-then-approval, provenance-laundering
resistance (Skeptic factual prose must not reach Writer), revision
exhaustion and its storage policy, audit metadata, the bounded revision
cap, the final_disposition matrix, and the no-LangGraph fallback.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools import writer_skeptic_graph as mod
from lisan.tools.memory_pipeline import run_memory_pipeline


def _patch_agents(monkeypatch, writer_cls, skeptic_cls):
    monkeypatch.setattr(mod, "WriterAgent", writer_cls)
    monkeypatch.setattr(mod, "SkepticAgent", skeptic_cls)
    monkeypatch.setattr(mod, "tokenize_principal_obj", lambda value, vault: value)


def test_approves_without_revision(monkeypatch, tmp_path: Path):
    calls = {"writer": 0, "skeptic": 0}

    class Writer:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["writer"] += 1
            return {"summary": text, "frontmatter": {}}

    class Skeptic:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["skeptic"] += 1
            return {"recommended_action": "approve", "approved": True}

    _patch_agents(monkeypatch, Writer, Skeptic)

    writer, skeptic, audit = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
    )

    assert writer["summary"] == "original source"
    assert skeptic["approved"] is True
    assert audit["revision_count"] == 0
    assert audit["disposition"] == "approved"
    assert audit["approved_immediately"] is True
    assert audit["issue_categories"] == []
    assert audit["max_revisions"] == 2
    assert calls == {"writer": 1, "skeptic": 1}


def test_revises_with_structured_editorial_feedback(monkeypatch, tmp_path: Path):
    writer_kwargs_seen = []
    skeptic_calls = 0

    class Writer:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            writer_kwargs_seen.append(kwargs)
            return {"summary": text, "frontmatter": {}, "pass": len(writer_kwargs_seen)}

    class Skeptic:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            nonlocal skeptic_calls
            skeptic_calls += 1
            if skeptic_calls < 3:
                return {
                    "recommended_action": "revise",
                    "approved": False,
                    "issues": [{"type": "interpretation", "message": "Separate fact from inference."}],
                }
            return {"recommended_action": "approve", "approved": True}

    _patch_agents(monkeypatch, Writer, Skeptic)

    writer, skeptic, audit = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={"retrieved_context": "context"},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert writer["pass"] == 3
    assert skeptic["approved"] is True
    assert audit["revision_count"] == 2
    assert audit["disposition"] == "approved"
    assert audit["approved_immediately"] is False
    assert audit["issue_categories"] == ["unmarked_inference"]
    assert len(writer_kwargs_seen) == 3
    # The raw skeptic dict is never passed through; only structured feedback.
    assert "skeptic_feedback" not in writer_kwargs_seen[1]
    feedback = json.loads(writer_kwargs_seen[1]["revision_feedback"])
    assert feedback["feedback_version"] == 1
    assert feedback["revision"] == 1
    assert len(feedback["issues"]) == 1
    issue = feedback["issues"][0]
    assert issue["category"] == "unmarked_inference"
    assert issue["category"] in mod.ISSUE_CATEGORIES
    assert issue["operation"] in mod.CATEGORY_OPERATIONS[issue["category"]]
    assert "target" in issue
    # The original INPUT is still the first positional arg (grounding).
    assert "editorial guidance, not evidence" in writer_kwargs_seen[1]["revision_instructions"]


def test_provenance_laundering_resistance(monkeypatch, tmp_path: Path):
    """Skeptic factual prose must never reach Writer, even as 'feedback'.

    The Skeptic invents a fact in observed_facts / interpretations /
    alternative_hypotheses. The structured feedback passed to Writer must
    contain the issue category but none of the invented prose.
    """
    writer_kwargs_seen = []
    skeptic_calls = 0
    invented = "The moon is made of cheese (skeptic invention)"

    class Writer:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            writer_kwargs_seen.append((text, kwargs))
            return {"summary": text, "frontmatter": {}}

    class Skeptic:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            nonlocal skeptic_calls
            skeptic_calls += 1
            if skeptic_calls == 1:
                return {
                    "recommended_action": "revise",
                    "approved": False,
                    "issues": [{"type": "interpretation", "message": "Separate fact from inference."}],
                    "observed_facts": [invented],
                    "interpretations": [invented],
                    "alternative_hypotheses": [invented],
                    "evidence_needed": [invented],
                    "claim_updates": [{"note": invented}],
                }
            return {"recommended_action": "approve", "approved": True}

    _patch_agents(monkeypatch, Writer, Skeptic)

    writer, skeptic, audit = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert audit["disposition"] == "approved"
    assert len(writer_kwargs_seen) == 2
    revision_kwargs = writer_kwargs_seen[1][1]
    feedback_raw = revision_kwargs["revision_feedback"]
    # The invented fact appears nowhere in anything Writer received.
    assert invented not in feedback_raw
    assert "cheese" not in feedback_raw
    feedback = json.loads(feedback_raw)
    assert mod.feedback_contains_factual_prose(feedback) is False
    # The previous draft and original input still travel along.
    assert "previous_writer_draft" in revision_kwargs
    assert writer_kwargs_seen[1][0] == "original source"


def test_exhaustion_yields_distinct_disposition(monkeypatch, tmp_path: Path):
    calls = {"writer": 0, "skeptic": 0}

    class Writer:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["writer"] += 1
            return {"summary": text, "frontmatter": {}, "pass": calls["writer"]}

    class Skeptic:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["skeptic"] += 1
            return {"recommended_action": "revise", "approved": False}

    _patch_agents(monkeypatch, Writer, Skeptic)

    writer, skeptic, audit = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert writer["pass"] == 3
    assert skeptic["recommended_action"] == "revise"
    assert audit["revision_count"] == 2
    assert audit["disposition"] == "revision_exhausted"
    assert audit["approved_immediately"] is False
    assert calls == {"writer": 3, "skeptic": 3}


def test_final_disposition_matrix():
    f = mod.final_disposition
    assert f({"recommended_action": "approve"}, 0, 2) == "approved"
    assert f({"recommended_action": "APPROVE"}, 1, 2) == "approved"
    assert f({"recommended_action": "hold"}, 0, 2) == "held"
    assert f({"recommended_action": "needs_revision"}, 0, 2) == "held"
    # "revise" below the cap is not terminal (the graph would loop).
    assert f({"recommended_action": "revise"}, 1, 2) == "revise"
    # "revise" at the cap is the distinct exhausted disposition.
    assert f({"recommended_action": "revise"}, 2, 2) == "revision_exhausted"
    # Unknown or empty actions fail closed.
    assert f({"recommended_action": "bogus"}, 0, 2) == "held"
    assert f({"recommended_action": ""}, 0, 2) == "held"
    assert f({}, 0, 2) == "held"
    assert f(None, 0, 2) == "held"


def test_structure_skeptic_feedback_drops_factual_fields():
    skeptic = {
        "recommended_action": "revise",
        "issues": [
            {"type": "uncertainty", "message": "Scope the uncertainty."},
            {"type": "made_up_type", "message": "Something editorial."},
            "not-a-dict",
        ],
        "observed_facts": ["invented fact"],
        "interpretations": ["invented interpretation"],
        "alternative_hypotheses": ["invented hypothesis"],
        "evidence_needed": ["invented need"],
        "claim_updates": [{"note": "invented note"}],
        "confidence_adjustments": [{"reason": "invented reason"}],
    }
    feedback = mod.structure_skeptic_feedback(skeptic, revision=1)
    assert feedback["feedback_version"] == 1
    assert feedback["revision"] == 1
    assert len(feedback["issues"]) == 2
    categories = {i["category"] for i in feedback["issues"]}
    assert categories == {"unscoped_uncertainty", "general_editorial"}
    assert mod.feedback_contains_factual_prose(feedback) is False
    raw = json.dumps(feedback)
    for leaked in ("invented fact", "invented interpretation", "invented hypothesis",
                   "invented need", "invented note", "invented reason"):
        assert leaked not in raw


def test_fallback_without_langgraph_matches_contract(monkeypatch, tmp_path: Path):
    """With LangGraph uninstallable, the deterministic fallback runs the
    identical bounded loop and returns the identical contract."""
    calls = {"writer": 0, "skeptic": 0}

    class Writer:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["writer"] += 1
            return {"summary": text, "frontmatter": {}}

    class Skeptic:
        def __init__(self, vault):
            self.vault = vault

        def run_json(self, text, **kwargs):
            calls["skeptic"] += 1
            if calls["skeptic"] == 1:
                return {
                    "recommended_action": "revise",
                    "approved": False,
                    "issues": [{"type": "placeholder", "message": "Fill the placeholder."}],
                }
            return {"recommended_action": "approve", "approved": True}

    _patch_agents(monkeypatch, Writer, Skeptic)
    # Make `from langgraph.graph import ...` raise ImportError.
    monkeypatch.setitem(sys.modules, "langgraph", None)
    monkeypatch.setitem(sys.modules, "langgraph.graph", None)

    writer, skeptic, audit = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert skeptic["approved"] is True
    assert audit["revision_count"] == 1
    assert audit["disposition"] == "approved"
    assert audit["issue_categories"] == ["placeholder"]
    assert calls == {"writer": 2, "skeptic": 2}


def test_exhaustion_storage_policy_withholds_fanout(tmp_path: Path):
    """End-to-end through run_memory_pipeline with a disposable vault:
    a skeptic that never approves exhausts revisions; the draft is stored
    with status revision_exhausted and no evidence/claim/state fanout runs.
    """
    with tempfile.TemporaryDirectory(prefix="lisan-ws-exhaust-") as tmp:
        root = Path(tmp)
        ensure_repo_layout(root)
        vault = vault_root(root)
        db_path = root / "lisan.sqlite"

        fake_listener = {
            "action": "full", "mode": "extraction", "memory_type": "evidence",
            "score": 8, "narrative_score": 8, "seed_score": 8, "reason": ["fixture"],
        }
        fake_writer = {
            "summary": "Friday ritual is pizza and a movie.",
            "significance": "high",
            "evidence_to_create": [{
                "title": "Friday ritual",
                "summary": "Friday ritual is pizza and a movie.",
                "observed_facts": ["Friday ritual is pizza and a movie."],
                "verbatim_excerpt": "Friday ritual is pizza and a movie.",
                "source_type": "conversation",
                "reliability": "high",
                "arena": "relational",
            }],
            "claims_to_create": [], "entities_to_create": [],
            "decisions_to_create": [], "open_loops_to_create": [],
            "state_updates": [],
        }

        class FakeListener:
            def __init__(self, *a, **k): pass
            def run_json(self, *a, **k): return dict(fake_listener)

        class FakeAssembler:
            def __init__(self, *a, **k): pass
            def run(self, *a, **k): return SimpleNamespace(text="")

        class FakeWriter:
            def __init__(self, *a, **k): pass
            def run_json(self, *a, **k): return dict(fake_writer)

        class FakeSkeptic:
            def __init__(self, *a, **k): pass
            def run_json(self, *a, **k):
                return {
                    "recommended_action": "revise",
                    "approved": False,
                    "issues": [{"type": "low_evidence", "message": "Needs grounding."}],
                }

        with patch("lisan.tools.memory_pipeline.ListenerAgent", FakeListener), \
             patch("lisan.tools.memory_pipeline.AssemblerAgent", FakeAssembler), \
             patch("lisan.tools.memory_pipeline.WriterAgent", FakeWriter), \
             patch("lisan.tools.writer_skeptic_graph.WriterAgent", FakeWriter), \
             patch("lisan.tools.writer_skeptic_graph.SkepticAgent", FakeSkeptic):
            result = run_memory_pipeline(
                vault=vault,
                text="Friday ritual is pizza and a movie.",
                conversation_id="exhaust-fixture",
                db_path=db_path,
                observed_response="Recorded.",
            )

        assert result.skeptic_disposition == "revision_exhausted"
        assert result.skeptic_approved is False
        assert result.skeptic_audit["revision_count"] == 2
        assert result.draft_path is not None
        draft_doc = load_markdown(Path(result.draft_path))
        assert draft_doc.frontmatter.get("status") == "revision_exhausted"
        assert draft_doc.frontmatter.get("skeptic_disposition") == "revision_exhausted"
        assert draft_doc.frontmatter.get("skeptic_audit", {}).get("revision_count") == 2
        # Storage policy: no evidence/claim/state fanout for exhausted drafts.
        assert list((vault / "evidence").rglob("*.md")) == []
