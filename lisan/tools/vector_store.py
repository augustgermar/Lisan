from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_config
from ..providers.embeddings import EmbeddingProvider


# embeddings.bin format (JSON lines):
#   line 1 (optional header): {"__meta__": {"model": str, "dimension": int, "version": 1}}
#   line n: {"id": <record id>, "embedding": [floats]}
#
# Switching the embedding model changes the dimension and therefore REQUIRES a
# full `rebuild-index`. The header records the model + dimension actually used
# so a stale index is detectable at query time (see VectorScorer.score).
META_KEY = "__meta__"
EMB_VERSION = 1


@dataclass(slots=True)
class EmbeddingIndex:
    model: str
    dimension: int
    vectors: dict[str, list[float]]

    @property
    def has_meta(self) -> bool:
        return self.dimension > 0


def write_embeddings(
    path: Path,
    records: list[tuple[str, list[float] | None]],
    *,
    model: str,
    dimension: int,
) -> None:
    """Write ``embeddings.bin`` with a metadata header line followed by one JSON
    line per record. Records whose vector is ``None`` (skipped, e.g. when the
    embedder was unreachable under ``unreachable_policy: skip``) are omitted —
    no vector is written for them."""
    lines = [json.dumps({META_KEY: {"model": model, "dimension": int(dimension), "version": EMB_VERSION}})]
    for record_id, vector in records:
        if vector is None:
            continue
        lines.append(json.dumps({"id": record_id, "embedding": vector}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# (mtime, size) -> EmbeddingIndex, keyed by resolved path string.
_INDEX_CACHE: dict[str, tuple[float, int, EmbeddingIndex]] = {}


def clear_index_cache() -> None:
    _INDEX_CACHE.clear()


def load_index(path: Path) -> EmbeddingIndex:
    """Load ``embeddings.bin`` into an in-memory ``{id: vector}`` map exactly
    once, cached keyed by file path + mtime + size. Subsequent calls within the
    same process reuse the parsed map until the file changes on disk."""
    if not path.exists():
        return EmbeddingIndex(model="none", dimension=0, vectors={})
    stat = path.stat()
    key = str(path.resolve())
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    model = "legacy"
    dimension = 0
    vectors: dict[str, list[float]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if META_KEY in payload:
                meta = payload[META_KEY] or {}
                model = str(meta.get("model", "legacy"))
                dimension = int(meta.get("dimension", 0) or 0)
                continue
            record_id = payload.get("id")
            embedding = payload.get("embedding")
            if record_id is None or not isinstance(embedding, list):
                continue
            vectors[str(record_id)] = [float(x) for x in embedding]

    # Legacy files (no header) have no recorded dimension; infer it from the
    # first vector so the dim-mismatch guard still has something to compare.
    if dimension == 0 and vectors:
        dimension = len(next(iter(vectors.values())))

    index = EmbeddingIndex(model=model, dimension=dimension, vectors=vectors)
    _INDEX_CACHE[key] = (stat.st_mtime, stat.st_size, index)
    return index


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Strict cosine similarity. Returns 0.0 when either vector is empty or the
    dimensions differ — it never truncates to the shorter length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    a_sq = 0.0
    b_sq = 0.0
    for x, y in zip(a, b):
        xf = float(x)
        yf = float(y)
        dot += xf * yf
        a_sq += xf * xf
        b_sq += yf * yf
    norm = math.sqrt(a_sq) * math.sqrt(b_sq)
    if norm == 0.0:
        return 0.0
    return dot / norm


@dataclass(slots=True)
class VectorScorer:
    """Scores candidates against a preloaded index using a query vector that was
    embedded exactly once. Construct via :func:`build_query_scorer` so the query
    embed and the file load each happen a single time per retrieval call."""

    query_vector: list[float] | None
    index: EmbeddingIndex
    mode_used: str  # semantic | hash | skip
    _warned: bool = field(default=False, repr=False)

    @property
    def active(self) -> bool:
        return bool(self.query_vector) and bool(self.index.vectors)

    def score(self, file_id: Any) -> float:
        if not self.query_vector or not self.index.vectors:
            return 0.0
        vector = self.index.vectors.get(str(file_id))
        if vector is None:
            return 0.0
        index_dim = self.index.dimension or len(vector)
        if len(self.query_vector) != index_dim:
            self._warn_dim_mismatch(index_dim)
            return 0.0
        return cosine_similarity(self.query_vector, vector)

    def _warn_dim_mismatch(self, index_dim: int) -> None:
        if self._warned:
            return
        self._warned = True
        print(
            "WARNING [embeddings] dimension mismatch: live query model emits "
            f"{len(self.query_vector or [])}-dim vectors but the index "
            f"(model='{self.index.model}') stores {index_dim}-dim vectors. "
            "The vector retrieval leg is being SKIPPED. Run `lisan rebuild-index` "
            "to rebuild the index with the current embedding model.",
            file=sys.stderr,
        )


def build_query_scorer(
    query: str,
    *,
    embeddings_file: Path,
    config: dict[str, Any] | None = None,
    provider: EmbeddingProvider | None = None,
) -> VectorScorer:
    """Embed the query once and load the index once, returning a scorer ready to
    rank candidates. ``mode_used`` reflects what actually happened
    (``semantic`` | ``hash`` | ``skip``) for telemetry."""
    config = config or load_config()
    provider = provider or EmbeddingProvider(config)
    query_embedding = provider.embed_query(query)
    index = load_index(embeddings_file)
    return VectorScorer(
        query_vector=query_embedding.vector,
        index=index,
        mode_used=query_embedding.mode_used,
    )
