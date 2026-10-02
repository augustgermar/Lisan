from __future__ import annotations

import io
import sys

import pytest

from lisan.tools import vector_store
from lisan.tools.vector_store import EmbeddingIndex, VectorScorer, _check_scorer, terminal_note


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def _fresh():
    vector_store.reset_terminal_notes()
    yield
    vector_store.reset_terminal_notes()


def _scorer(vec, index):
    return VectorScorer(query_vector=vec, index=index, mode_used="semantic" if vec else "skip")


def _run(monkeypatch, scorer, tty=True):
    err = _Tty() if tty else io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    _check_scorer(scorer, vector_store.Path("embeddings.bin"))
    return err.getvalue()


def test_note_when_embedder_unavailable(monkeypatch):
    out = _run(monkeypatch, _scorer(None, EmbeddingIndex("m", 3, {"a": [1, 0, 0]})))
    assert "semantic search is OFF" in out and "fastembed" in out


def test_note_when_index_empty(monkeypatch):
    out = _run(monkeypatch, _scorer([1, 0, 0], EmbeddingIndex("none", 0, {})))
    assert "no vector index" in out


def test_note_on_dimension_mismatch(monkeypatch):
    out = _run(monkeypatch, _scorer([1, 0], EmbeddingIndex("m", 3, {"a": [1, 0, 0]})))
    assert "3-dim" in out and "2-dim" in out


def test_silent_when_healthy(monkeypatch):
    assert _run(monkeypatch, _scorer([1, 0, 0], EmbeddingIndex("m", 3, {"a": [1, 0, 0]}))) == ""


def test_never_written_when_stderr_is_not_a_tty(monkeypatch):
    """Services log stderr to a file; the note must stay a terminal-only thing."""
    assert _run(monkeypatch, _scorer(None, EmbeddingIndex("m", 3, {})), tty=False) == ""


def test_once_per_reason(monkeypatch):
    err = _Tty()
    monkeypatch.setattr(sys, "stderr", err)
    terminal_note("x", "fix")
    terminal_note("x", "fix")
    assert err.getvalue().count("semantic search is OFF") == 1
