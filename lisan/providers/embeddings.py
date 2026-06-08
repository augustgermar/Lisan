from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from ..config import embedding_settings, load_config
from ..utils import hash_embedding


# Warn-once registry so a down embedder during retrieval does not spam the log
# on every query. Keyed by a short reason tag.
_WARNED: set[str] = set()


class FastEmbedUnavailable(RuntimeError):
    """Raised when the optional ``fastembed`` backend is selected but the
    package is not importable. Treated exactly like an unreachable embedder."""


# Held FastEmbed ``TextEmbedding`` instances, keyed by (model_name, cache_dir).
# Instantiation is the cold-start cost (it loads/downloads ONNX weights), so the
# object is created exactly once per process and reused by every query/record.
_FASTEMBED_MODELS: dict[tuple[str, str], Any] = {}

# Cached "package not installed" state so we do not re-attempt the import (and
# re-warn) on every call once we have learned it is missing.
_FASTEMBED_UNAVAILABLE: bool = False


def _warn(tag: str, message: str, *, once: bool = True) -> None:
    if once:
        if tag in _WARNED:
            return
        _WARNED.add(tag)
    print(f"WARNING [embeddings] {message}", file=sys.stderr)


def reset_warnings() -> None:
    """Test/diagnostic helper: clear the warn-once registry."""
    _WARNED.clear()


def reset_provider_state() -> None:
    """Test/diagnostic helper: clear per-process embedding caches (warn-once
    registry, the held FastEmbed instance(s), and the unavailable flag)."""
    global _FASTEMBED_UNAVAILABLE
    _WARNED.clear()
    _FASTEMBED_MODELS.clear()
    _FASTEMBED_UNAVAILABLE = False
    _ST_MODELS.clear()


@dataclass(slots=True)
class IndexEmbedding:
    """Result of embedding a batch of record texts for the index."""

    vectors: list[list[float] | None]
    mode_used: str  # semantic | hash | skip
    model: str
    dimension: int
    reachable: bool


@dataclass(slots=True)
class QueryEmbedding:
    """Result of embedding a single query for retrieval."""

    vector: list[float] | None
    mode_used: str  # semantic | hash | skip
    dimension: int
    reachable: bool


# Cache for lazily-loaded sentence-transformers models, keyed by model name.
_ST_MODELS: dict[str, Any] = {}


class EmbeddingProvider:
    """Local-first semantic embedding provider.

    Backends, selected by ``retrieval.embeddings.provider``:

    - ``fastembed`` (default, recommended): an in-process ONNX embedder
      (Qdrant's FastEmbed, no PyTorch, CPU-only). Optional dependency —
      ``pip install lisan[embeddings]``. Installing the extra is the
      activation: semantic retrieval turns on with no config change. A base
      install without the extra treats it as unreachable and degrades to
      keyword-only via ``unreachable_policy``.
    - ``local`` / hosted: an OpenAI-compatible ``POST {base_url}/v1/embeddings``
      endpoint (llama.cpp / LM Studio / Ollama-compatible, or hosted APIs).
    - ``sentence-transformers``: a secondary in-process backend behind a lazy
      import; never a hard dependency (and torch is not in the extra).

    The deterministic ``hash_embedding`` fallback (see :mod:`lisan.utils`) is
    used only when ``mode`` is ``hash`` or the embedder is unreachable and
    ``unreachable_policy`` is ``hash``.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or load_config()
        self.settings = embedding_settings(self.config)

    # -- public, schema-shaped API ---------------------------------------

    @property
    def mode(self) -> str:
        return str(self.settings.get("mode", "auto"))

    @property
    def hash_dimensions(self) -> int:
        return int(self.settings.get("hash_dimensions", 32))

    def embed_text(self, text: str) -> list[float]:
        """Embed a single string. Always returns a vector: semantic when the
        backend is reachable, otherwise a deterministic hash fallback. Use
        :meth:`embed_query` when you need to honour the skip policy."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of strings using the endpoint's native array batching.

        Always returns one vector per input. This is the low-level convenience
        API; it falls back to ``hash_embedding`` on any failure regardless of
        ``unreachable_policy`` so callers that just want vectors never get
        ``None``."""
        if not texts:
            return []
        if self.mode == "hash":
            return [self._hash(t) for t in texts]
        remote = self._attempt_remote(texts)
        if remote is not None:
            return remote
        return [self._hash(t) for t in texts]

    # -- index-time API (honours unreachable_policy) ----------------------

    def embed_records(self, texts: list[str]) -> IndexEmbedding:
        """Embed record texts for the index, honouring ``unreachable_policy``.

        When the embedder is unreachable and the policy is ``skip``, returns
        ``None`` for every vector and ``mode_used="skip"`` so the caller writes
        no vectors and flags the records pending instead of poisoning a
        semantic index with hash vectors."""
        if self.mode == "hash":
            vectors = [self._hash(t) for t in texts]
            return IndexEmbedding(vectors, "hash", "hash-fallback", self.hash_dimensions, reachable=False)

        remote = self._attempt_remote(texts)
        if remote is not None:
            dim = len(remote[0]) if remote and remote[0] else 0
            return IndexEmbedding(remote, "semantic", str(self.settings.get("model", "")), dim, reachable=True)

        # Unreachable.
        if str(self.settings.get("unreachable_policy", "skip")) == "hash":
            vectors = [self._hash(t) for t in texts]
            return IndexEmbedding(vectors, "hash", "hash-fallback", self.hash_dimensions, reachable=False)
        return IndexEmbedding([None] * len(texts), "skip", "none", 0, reachable=False)

    # -- query-time API (honours unreachable_policy) ----------------------

    def embed_query(self, text: str) -> QueryEmbedding:
        """Embed a query, honouring ``unreachable_policy``. When unreachable and
        the policy is ``skip``, returns ``vector=None`` so retrieval drops the
        vector leg for this query."""
        if self.mode == "hash":
            vec = self._hash(text)
            return QueryEmbedding(vec, "hash", len(vec), reachable=False)

        remote = self._attempt_remote([text], is_query=True)
        if remote is not None:
            vec = remote[0]
            return QueryEmbedding(vec, "semantic", len(vec), reachable=True)

        if str(self.settings.get("unreachable_policy", "skip")) == "hash":
            vec = self._hash(text)
            return QueryEmbedding(vec, "hash", len(vec), reachable=False)
        return QueryEmbedding(None, "skip", 0, reachable=False)

    # -- internals --------------------------------------------------------

    def _hash(self, text: str) -> list[float]:
        return hash_embedding(text, self.hash_dimensions)

    def _attempt_remote(self, texts: list[str], *, is_query: bool = False) -> list[list[float]] | None:
        """Attempt to embed via the configured backend. Returns vectors on
        success, or ``None`` when the backend is unreachable/unusable.

        ``is_query`` selects the query vs passage form where the backend draws a
        distinction (FastEmbed's BGE model); the HTTP and sentence-transformers
        paths ignore it.

        Reachability fast-fails: a refused connection raises immediately and
        does NOT wait out ``timeout_seconds`` (that timeout only applies to a
        server that accepts the connection but hangs). A missing optional
        backend (FastEmbed not installed) fast-fails the same way."""
        provider = str(self.settings.get("provider", "local"))
        try:
            if provider == "fastembed":
                vectors = self._embed_fastembed(texts, is_query=is_query)
            elif provider == "sentence-transformers":
                vectors = self._embed_sentence_transformers(texts)
            else:
                vectors = self._embed_http(texts)
        except FastEmbedUnavailable as exc:
            level = "ERROR" if self.mode == "semantic" else "INFO"
            _warn(
                "fastembed-missing",
                f"provider 'fastembed' selected but the 'fastembed' package is not installed "
                f"({exc}). Install it with `pip install lisan[embeddings]` to enable in-process "
                f"semantic embeddings; running keyword-only via "
                f"unreachable_policy='{self.settings.get('unreachable_policy', 'skip')}'. [{level}]",
            )
            return None
        except Exception as exc:  # unreachable, bad response, etc.
            tag = f"unreachable:{provider}"
            level = "ERROR" if self.mode == "semantic" else "INFO"
            _warn(
                tag,
                f"embedder unreachable via provider '{provider}' ({exc.__class__.__name__}: {exc}); "
                f"applying unreachable_policy='{self.settings.get('unreachable_policy', 'skip')}'. [{level}]",
            )
            return None
        if not vectors:
            return None
        self._check_dimension_hint(len(vectors[0]) if vectors[0] else 0)
        return vectors

    def _embed_http(self, texts: list[str]) -> list[list[float]]:
        url = self._endpoint()
        headers = {"Content-Type": "application/json"}
        api_key_env = self.settings.get("api_key_env")
        if api_key_env:
            api_key = os.getenv(str(api_key_env))
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": self.settings.get("model"), "input": list(texts)}
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method="POST")
        timeout = int(self.settings.get("timeout_seconds", 30))
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        return _parse_openai_embeddings(body, expected=len(texts))

    def _embed_sentence_transformers(self, texts: list[str]) -> list[list[float]]:
        model_name = str(self.settings.get("model", ""))
        model = _ST_MODELS.get(model_name)
        if model is None:
            from sentence_transformers import SentenceTransformer  # lazy, optional

            model = SentenceTransformer(model_name)
            _ST_MODELS[model_name] = model
        vectors = model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, vec)) for vec in vectors]

    def _embed_fastembed(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        """Embed via FastEmbed's in-process ONNX model. The model object is
        instantiated exactly once per process (see :func:`_get_fastembed_model`)
        and reused for every query and record."""
        model_name = str(self.settings.get("model", "BAAI/bge-small-en-v1.5"))
        model = _get_fastembed_model(model_name, self._fastembed_cache_dir())
        batch_size = max(1, int(self.settings.get("batch_size", 64) or 64))

        query_prefix = self.settings.get("query_prefix")
        passage_prefix = self.settings.get("passage_prefix")
        manual_prefix = query_prefix is not None or passage_prefix is not None

        documents = list(texts)
        # Prefer FastEmbed's dedicated query/passage methods (they apply the
        # model's documented convention internally) unless the user has set
        # explicit prefixes, or the installed version lacks those methods.
        if not manual_prefix and is_query and hasattr(model, "query_embed"):
            raw = model.query_embed(documents, batch_size=batch_size)
        elif not manual_prefix and not is_query and hasattr(model, "passage_embed"):
            raw = model.passage_embed(documents, batch_size=batch_size)
        else:
            prefix = query_prefix if is_query else passage_prefix
            if prefix is None:
                prefix = ""  # native methods unavailable and no override given
            raw = model.embed([f"{prefix}{text}" for text in documents], batch_size=batch_size)

        # .embed()/.query_embed()/.passage_embed() return a GENERATOR of numpy
        # arrays — materialize, then convert each to list[float] for the
        # JSON-lines store.
        return [[float(value) for value in vector] for vector in list(raw)]

    def _fastembed_cache_dir(self) -> str:
        """Resolve the FastEmbed weight cache location: explicit config first,
        then ``FASTEMBED_CACHE_PATH``, then a predictable Lisan cache path (never
        the system temp dir)."""
        configured = self.settings.get("cache_dir")
        if configured:
            cache_dir = Path(str(configured)).expanduser()
        elif os.environ.get("FASTEMBED_CACHE_PATH"):
            cache_dir = Path(os.environ["FASTEMBED_CACHE_PATH"]).expanduser()
        else:
            cache_dir = Path.home() / ".cache" / "lisan" / "fastembed"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir)

    def _endpoint(self) -> str:
        base = str(self.settings.get("base_url", "")).rstrip("/")
        if base.endswith("/embeddings"):
            return base
        if base.endswith("/v1"):
            return base + "/embeddings"
        return base + "/v1/embeddings"

    def _check_dimension_hint(self, observed: int) -> None:
        hint = int(self.settings.get("dimensions", 0) or 0)
        if observed and hint and observed != hint:
            _warn(
                f"dimhint:{observed}:{hint}",
                f"embedder returned dimension {observed} but config hint is {hint}; "
                f"the actual dimension {observed} is authoritative and will be written to the index header.",
            )


def _get_fastembed_model(model_name: str, cache_dir: str) -> Any:
    """Return the held FastEmbed ``TextEmbedding`` for ``(model_name, cache_dir)``,
    instantiating it exactly once per process. Raises :class:`FastEmbedUnavailable`
    (cached) when the optional package is not installed."""
    global _FASTEMBED_UNAVAILABLE
    if _FASTEMBED_UNAVAILABLE:
        raise FastEmbedUnavailable("fastembed package not installed")
    key = (model_name, cache_dir or "")
    model = _FASTEMBED_MODELS.get(key)
    if model is not None:
        return model
    try:
        from fastembed import TextEmbedding  # lazy, optional ([embeddings] extra)
    except ImportError as exc:
        _FASTEMBED_UNAVAILABLE = True
        raise FastEmbedUnavailable(str(exc)) from exc
    model = TextEmbedding(model_name=model_name, cache_dir=cache_dir or None)
    _FASTEMBED_MODELS[key] = model
    return model


def _parse_openai_embeddings(body: dict[str, Any], *, expected: int) -> list[list[float]]:
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("embeddings response missing 'data' array")
    ordered = sorted(data, key=lambda item: int(item.get("index", 0)) if isinstance(item, dict) else 0)
    vectors: list[list[float]] = []
    for item in ordered:
        if not isinstance(item, dict):
            raise RuntimeError("embeddings response item is not an object")
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("embeddings response item missing 'embedding'")
        vectors.append([float(x) for x in embedding])
    if len(vectors) != expected:
        raise RuntimeError(f"embeddings response returned {len(vectors)} vectors for {expected} inputs")
    return vectors
