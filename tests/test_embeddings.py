from __future__ import annotations

import json
import sys
import tempfile
import types
from array import array
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from lisan.providers import embeddings as embeddings_module
from lisan.providers.embeddings import (
    EmbeddingProvider,
    QueryEmbedding,
    _parse_openai_embeddings,
)
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.retrieval import retrieve_context
from lisan.tools.vector_store import (
    EmbeddingIndex,
    VectorScorer,
    clear_index_cache,
    cosine_similarity,
    load_index,
    write_embeddings,
)
from lisan.utils import hash_embedding

# Captured before the autouse offline fixture (conftest) replaces it, so tests
# that need the genuine transport can restore it.
_REAL_ATTEMPT_REMOTE = EmbeddingProvider._attempt_remote


def _cfg(**embeddings):
    return {"retrieval": {"embeddings": embeddings}}


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _fake_urlopen(dim: int):
    def _open(req, timeout=None):
        payload = json.loads(req.data.decode("utf-8"))
        inputs = payload["input"]
        data = [{"index": i, "embedding": [float(i + 1)] * dim} for i in range(len(inputs))]
        return _FakeResponse(json.dumps({"data": data, "model": payload.get("model")}).encode("utf-8"))

    return _open


# --- embed_text / embed_batch shape and dimension -----------------------


def test_embed_batch_shape_and_dimension(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    monkeypatch.setattr(embeddings_module.request, "urlopen", _fake_urlopen(dim=8))

    provider = EmbeddingProvider(_cfg(mode="auto", provider="local", dimensions=8))
    vectors = provider.embed_batch(["a", "bb", "ccc"])

    assert len(vectors) == 3
    assert all(len(v) == 8 for v in vectors)
    assert len(provider.embed_text("solo")) == 8


def test_parse_openai_embeddings_orders_by_index():
    body = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    vectors = _parse_openai_embeddings(body, expected=2)
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


# --- fallback when the backend is unreachable ---------------------------


def test_embed_batch_falls_back_to_hash_when_unreachable(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)

    def _boom(req, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(embeddings_module.request, "urlopen", _boom)

    provider = EmbeddingProvider(_cfg(mode="auto", provider="local", hash_dimensions=32))
    vectors = provider.embed_batch(["hello"])

    assert vectors == [hash_embedding("hello", 32)]
    assert len(vectors[0]) == 32


def test_embed_query_skip_policy_drops_vector_when_unreachable(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    monkeypatch.setattr(embeddings_module.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(URLError("x")))

    provider = EmbeddingProvider(_cfg(mode="auto", provider="local", unreachable_policy="skip"))
    result = provider.embed_query("hello")

    assert result.vector is None
    assert result.mode_used == "skip"
    assert result.reachable is False


def test_embed_query_hash_policy_substitutes_hash_when_unreachable(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    monkeypatch.setattr(embeddings_module.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(URLError("x")))

    provider = EmbeddingProvider(_cfg(mode="auto", provider="local", unreachable_policy="hash", hash_dimensions=16))
    result = provider.embed_query("hello")

    assert result.mode_used == "hash"
    assert result.vector is not None and len(result.vector) == 16


def test_mode_hash_never_calls_network(monkeypatch):
    def _boom(req, timeout=None):  # pragma: no cover - must never run
        raise AssertionError("mode=hash must not touch the network")

    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    monkeypatch.setattr(embeddings_module.request, "urlopen", _boom)

    provider = EmbeddingProvider(_cfg(mode="hash", hash_dimensions=32))
    assert provider.embed_batch(["x", "y"]) == [hash_embedding("x", 32), hash_embedding("y", 32)]


# --- cosine dim-mismatch is skipped, never truncated --------------------


def test_cosine_dim_mismatch_returns_zero():
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([], [1.0]) == 0.0


def test_vector_scorer_skips_on_dimension_mismatch():
    index = EmbeddingIndex(model="m", dimension=4, vectors={"x": [1.0, 0.0, 0.0, 0.0]})
    # Query is 3-dim while the index stores 4-dim vectors: must skip, not truncate.
    scorer = VectorScorer(query_vector=[1.0, 0.0, 0.0], index=index, mode_used="semantic")
    assert scorer.score("x") == 0.0

    matched = VectorScorer(query_vector=[1.0, 0.0, 0.0, 0.0], index=index, mode_used="semantic")
    assert matched.score("x") == pytest.approx(1.0)
    assert matched.score("missing") == 0.0


# --- embeddings.bin round-trips dimension metadata ----------------------


def test_embeddings_bin_roundtrips_metadata_and_skips_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "embeddings.bin"
        write_embeddings(
            path,
            [("a", [1.0, 0.0, 0.0, 0.0]), ("b", None), ("c", [0.0, 1.0, 0.0, 0.0])],
            model="test-embed",
            dimension=4,
        )
        clear_index_cache()
        index = load_index(path)

    assert index.model == "test-embed"
    assert index.dimension == 4
    assert set(index.vectors) == {"a", "c"}  # the None ("pending") record wrote no vector
    assert index.vectors["a"] == [1.0, 0.0, 0.0, 0.0]


# --- query embedded once per retrieval, not once per candidate ----------


def _write_episode(vault: Path, slug: str, summary: str, body: str) -> None:
    fm = {
        "id": f"episode.{slug}",
        "type": "episode",
        "created": "2026-06-01",
        "updated": "2026-06-01",
        "status": "active",
        "significance": "medium",
        "domain_primary": "work",
        "domain_secondary": [],
        "privacy": "personal",
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": summary,
        "links": [],
        "confidence": "medium",
        "confidence_basis": "test",
        "last_confirmed": "2026-06-01",
        "review_after": "2026-12-01",
    }
    out = vault / "episodes" / f"{slug}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("---\n" + json.dumps(fm, indent=2) + "\n---\n\n" + body + "\n", encoding="utf-8")


def test_query_embedded_once_per_retrieval(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_repo_layout(root)
        vault = vault_root(root)
        db_path = root / "lisan.sqlite"
        embeddings_file = root / "embeddings.bin"

        _write_episode(vault, "alpha", "Alpha project kickoff", "We launched the alpha project rollout plan at work.")
        _write_episode(vault, "beta", "Beta review meeting", "The beta review meeting covered the rollout plan and budget.")
        _write_episode(vault, "gamma", "Gamma retro notes", "Retro notes about the rollout plan and team morale.")

        rebuild_index(vault=vault, db_path=db_path, embeddings_file=embeddings_file)

        # Give the index real (matching-dimension) vectors so the vector leg is
        # active and the scorer is exercised against every candidate.
        write_embeddings(
            embeddings_file,
            [
                ("episode.alpha", [1.0, 0.0, 0.0, 0.0]),
                ("episode.beta", [0.0, 1.0, 0.0, 0.0]),
                ("episode.gamma", [0.0, 0.0, 1.0, 0.0]),
            ],
            model="test-embed",
            dimension=4,
        )
        clear_index_cache()

        embed_query = MagicMock(return_value=QueryEmbedding([1.0, 0.0, 0.0, 0.0], "semantic", 4, True))
        monkeypatch.setattr(EmbeddingProvider, "embed_query", embed_query)

        retrieve_context("rollout plan", domain="work", vault=vault, db_path=db_path)

        # Exactly one embed for the whole retrieval, regardless of the three
        # candidates that were scored against the preloaded map.
        assert embed_query.call_count == 1


# --- FastEmbed in-process backend (mocked — never installs/loads) -------


def _install_fake_fastembed(monkeypatch, *, dim=5):
    """Inject a fake ``fastembed`` module so the lazy ``from fastembed import
    TextEmbedding`` resolves without the real package. ``array('f', ...)`` stands
    in for FastEmbed's numpy ndarray output (same iteration contract)."""
    calls = {"init": 0, "query": 0, "passage": 0, "embed": 0, "embed_docs": []}
    module = types.ModuleType("fastembed")

    class FakeTextEmbedding:
        def __init__(self, model_name=None, cache_dir=None, **kwargs):
            calls["init"] += 1
            self.model_name = model_name
            self.cache_dir = cache_dir

        def _vectors(self, documents):
            for i, _doc in enumerate(documents):
                yield array("f", [float(i)] + [0.1] * (dim - 1))

        def query_embed(self, query, batch_size=256, **kwargs):
            calls["query"] += 1
            documents = [query] if isinstance(query, str) else list(query)
            return self._vectors(documents)

        def passage_embed(self, texts, batch_size=256, **kwargs):
            calls["passage"] += 1
            documents = [texts] if isinstance(texts, str) else list(texts)
            return self._vectors(documents)

        def embed(self, documents, batch_size=256, **kwargs):
            calls["embed"] += 1
            documents = list(documents)
            calls["embed_docs"].append(documents)
            return self._vectors(documents)

    module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return calls


def test_fastembed_missing_warns_once_and_falls_back(monkeypatch, capsys):
    # Restore the genuine dispatch (conftest stubs it offline) and ensure no fake
    # fastembed is present, so the real lazy import raises ImportError.
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    embeddings_module.reset_provider_state()

    provider = EmbeddingProvider(_cfg(mode="auto", provider="fastembed", unreachable_policy="skip"))
    results = [provider.embed_query("question one"), provider.embed_query("q2"), provider.embed_query("q3")]

    # All fall back per skip policy; the missing package never crashes.
    assert all(r.vector is None and r.mode_used == "skip" for r in results)
    # The unavailable state is cached so the import is not retried.
    assert embeddings_module._FASTEMBED_UNAVAILABLE is True
    # Exactly one informational warning across the three calls.
    err = capsys.readouterr().err
    assert err.count("pip install lisan[embeddings]") == 1


def test_fastembed_model_instantiated_once(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    calls = _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    provider = EmbeddingProvider(_cfg(mode="auto", provider="fastembed"))
    for _ in range(5):
        provider.embed_query("a query")
    provider.embed_records(["doc one", "doc two"])

    # The model object is constructed exactly once for the whole process, not
    # once per query/record. (Default config applies prefixes via embed().)
    assert calls["init"] == 1
    assert calls["embed"] == 6  # 5 queries + 1 batched records call


def test_fastembed_default_applies_bge_query_prefix(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    calls = _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    # Default config carries BGE's documented convention: queries get the
    # instruction prefix, passages get none. FastEmbed's native methods are a
    # no-op for this model, so the distinction must come from the prefix.
    provider = EmbeddingProvider(_cfg(mode="auto", provider="fastembed"))
    provider.embed_query("find the rollout plan")
    provider.embed_records(["the rollout plan was approved"])

    assert calls["query"] == 0 and calls["passage"] == 0  # native methods bypassed
    assert calls["embed_docs"][0] == ["Represent this sentence for searching relevant passages: find the rollout plan"]
    assert calls["embed_docs"][1] == ["the rollout plan was approved"]


def test_fastembed_null_prefixes_use_native_methods(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    calls = _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    # Nulling both prefixes defers to FastEmbed's native query/passage methods.
    provider = EmbeddingProvider(
        _cfg(mode="auto", provider="fastembed", query_prefix=None, passage_prefix=None)
    )
    provider.embed_query("find the rollout plan")
    provider.embed_records(["the rollout plan was approved"])

    assert calls["query"] == 1
    assert calls["passage"] == 1
    assert calls["embed"] == 0


def test_fastembed_manual_prefixes_use_embed_with_prefixed_text(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    calls = _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    provider = EmbeddingProvider(
        _cfg(mode="auto", provider="fastembed", query_prefix="Q: ", passage_prefix="P: ")
    )
    provider.embed_query("hello")
    provider.embed_records(["world"])

    # Explicit prefixes switch off the native methods and prefix the inputs.
    assert calls["query"] == 0 and calls["passage"] == 0
    assert calls["embed_docs"] == [["Q: hello"], ["P: world"]]


def test_fastembed_observed_dimension_is_authoritative(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    # Config hints 384 (the BGE default) but the (fake) model returns dim 5.
    provider = EmbeddingProvider(_cfg(mode="auto", provider="fastembed", dimensions=384))
    outcome = provider.embed_records(["alpha", "beta"])

    assert outcome.mode_used == "semantic"
    assert outcome.dimension == 5  # observed wins over the 384 hint
    # numpy/array -> list[float] conversion happened.
    assert all(isinstance(v, list) and all(isinstance(x, float) for x in v) for v in outcome.vectors)

    # Round-trips through the JSON-lines store with the observed dimension.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "embeddings.bin"
        records = list(zip(["alpha", "beta"], outcome.vectors))
        write_embeddings(path, records, model=outcome.model, dimension=outcome.dimension)
        clear_index_cache()
        index = load_index(path)

    assert index.dimension == 5
    assert set(index.vectors) == {"alpha", "beta"}
    assert index.vectors["beta"][0] == pytest.approx(1.0)


def test_fastembed_query_embedding_is_semantic(monkeypatch):
    monkeypatch.setattr(EmbeddingProvider, "_attempt_remote", _REAL_ATTEMPT_REMOTE)
    _install_fake_fastembed(monkeypatch, dim=5)
    embeddings_module.reset_provider_state()

    provider = EmbeddingProvider(_cfg(mode="auto", provider="fastembed"))
    result = provider.embed_query("a question")

    assert result.mode_used == "semantic"
    assert result.reachable is True
    assert result.vector is not None and len(result.vector) == 5
