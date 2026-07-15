from __future__ import annotations

import pytest

from lisan.providers import embeddings as embeddings_module
from lisan.tools import telegram_bot as telegram_bot_module


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


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    """No test may ever reach the real Telegram API.

    Failure escalation notifies the owner from deep inside the job worker,
    reading the developer's real config.json if nothing intervenes — a test
    that exercises a terminal failure must not page the owner's phone. Stub
    the single outbound seam; tests that observe delivery use ``send_fn``
    or patch ``_deliver_owner_message`` themselves, above this seam.
    """

    def _blocked(token, method, params, timeout=15):
        raise AssertionError("test attempted a real Telegram API call")

    monkeypatch.setattr(telegram_bot_module, "_telegram_api", _blocked)
