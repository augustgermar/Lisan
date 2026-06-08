from __future__ import annotations

import pytest

from lisan.providers import embeddings as embeddings_module


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    """Make the semantic embedder behave as unreachable for the whole suite.

    Tests must not depend on whether a developer happens to have a local
    embedding server listening on 127.0.0.1:8080. With the embedder
    unreachable and the default ``unreachable_policy: skip``, the vector
    retrieval leg is simply dropped — deterministic and offline. Tests that
    exercise the semantic path explicitly construct their own provider /
    mocks and are unaffected by this default.
    """
    monkeypatch.setattr(
        embeddings_module.EmbeddingProvider,
        "_attempt_remote",
        lambda self, texts, **kwargs: None,
    )
    embeddings_module.reset_provider_state()
    yield
    embeddings_module.reset_provider_state()
