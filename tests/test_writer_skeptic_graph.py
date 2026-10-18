from __future__ import annotations

from pathlib import Path

from lisan.tools import writer_skeptic_graph as mod


def test_writer_skeptic_graph_approves_without_revision(monkeypatch, tmp_path: Path):
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

    monkeypatch.setattr(mod, "WriterAgent", Writer)
    monkeypatch.setattr(mod, "SkepticAgent", Skeptic)
    monkeypatch.setattr(mod, "tokenize_principal_obj", lambda value, vault: value)

    writer, skeptic, revisions = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
    )

    assert writer["summary"] == "original source"
    assert skeptic["approved"] is True
    assert revisions == 0
    assert calls == {"writer": 1, "skeptic": 1}


def test_writer_skeptic_graph_revises_with_editorial_context(monkeypatch, tmp_path: Path):
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

    monkeypatch.setattr(mod, "WriterAgent", Writer)
    monkeypatch.setattr(mod, "SkepticAgent", Skeptic)
    monkeypatch.setattr(mod, "tokenize_principal_obj", lambda value, vault: value)

    writer, skeptic, revisions = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={"retrieved_context": "context"},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert writer["pass"] == 3
    assert skeptic["approved"] is True
    assert revisions == 2
    assert len(writer_kwargs_seen) == 3
    assert "skeptic_feedback" not in writer_kwargs_seen[0]
    assert "previous_writer_draft" in writer_kwargs_seen[1]
    assert "editorial guidance, not evidence" in writer_kwargs_seen[1]["revision_instructions"]


def test_writer_skeptic_graph_stops_at_revision_cap(monkeypatch, tmp_path: Path):
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

    monkeypatch.setattr(mod, "WriterAgent", Writer)
    monkeypatch.setattr(mod, "SkepticAgent", Skeptic)
    monkeypatch.setattr(mod, "tokenize_principal_obj", lambda value, vault: value)

    writer, skeptic, revisions = mod.run_writer_skeptic_graph(
        vault=tmp_path,
        text="original source",
        writer_task="episode_core",
        writer_kwargs={},
        skeptic_kwargs={},
        max_revisions=2,
    )

    assert writer["pass"] == 3
    assert skeptic["recommended_action"] == "revise"
    assert revisions == 2
    assert calls == {"writer": 3, "skeptic": 3}
