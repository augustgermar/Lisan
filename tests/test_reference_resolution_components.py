from __future__ import annotations

from unittest.mock import patch

from lisan.providers.embeddings import EmbeddingProvider
from lisan.tools.reference_resolution import resolve_reference


def _fake_embed_text(self, text: str) -> list[float]:
    lowered = str(text).lower()
    if "alpha beta gamma" in lowered:
        return [1.0, 0.0]
    if "semantic-anchor" in lowered:
        return [0.996, 0.09]
    if "lexical-anchor" in lowered:
        return [0.84, 0.54]
    return [0.0, 0.0]


def test_resolve_reference_preserves_winner_components() -> None:
    query = "alpha beta gamma"
    semantic_candidate = {
        "id": "cand.semantic",
        "name": "alpha semantic-anchor",
        "summary": "alpha semantic-anchor",
    }
    lexical_candidate = {
        "id": "cand.lexical",
        "name": "alpha beta lexical-anchor",
        "summary": "alpha beta lexical-anchor",
    }

    with patch.object(EmbeddingProvider, "embed_text", new=_fake_embed_text):
        winner = resolve_reference(query, [semantic_candidate, lexical_candidate])
        semantic_only = resolve_reference(query, [semantic_candidate])
        lexical_only = resolve_reference(query, [lexical_candidate])

    assert winner.candidate is not None
    assert winner.candidate["id"] == "cand.semantic"
    assert winner.score == semantic_only.score
    assert winner.exact == semantic_only.exact
    assert winner.lexical == semantic_only.lexical
    assert winner.semantic == semantic_only.semantic
    assert abs(semantic_only.score - lexical_only.score) < 0.05
    assert semantic_only.lexical != lexical_only.lexical
    assert semantic_only.semantic != lexical_only.semantic
