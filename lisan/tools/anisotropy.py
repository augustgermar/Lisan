"""Embedding anisotropy correction — "all-but-the-top" (Mu & Viswanath 2018).

Transformer embedding models emit vectors that occupy a narrow cone of the
embedding space rather than spreading over the unit sphere, so cosine
similarities cluster in a compressed band and a few dominant directions
drown the semantic signal. The classic fix: subtract the corpus mean and
the projection onto the top principal component(s), then renormalize —

    v' = v - mean
    for each pc:  v' = v' - (v' . pc) pc
    v' = v' / ||v'||

Applied consistently to every stored vector AND the query vector, so both
live in the same corrected space. Calibration is computed from the loaded
corpus itself (deterministic given the corpus; recomputed whenever the
index file changes, riding the same cache), so there is nothing to
persist and nothing to drift. RRF fusion is rank-based, so the benefit
arrives as better *ordering*, not changed thresholds.

Deliberately conservative: k=1 component by default; corpora smaller than
``min_corpus`` are left uncorrected (a top PC estimated from a handful of
vectors is noise); the deterministic hash fallback space is not a cone
and is left alone; and without numpy the correction is skipped silently —
it is an enhancement, never a dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULTS: dict[str, Any] = {"enabled": True, "components": 1, "min_corpus": 16}

_POWER_ITERATIONS = 30


def anisotropy_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    embeddings_cfg = ((config or {}).get("retrieval") or {}).get("embeddings") or {}
    out.update(embeddings_cfg.get("anisotropy") or {})
    return out


@dataclass(slots=True)
class Calibration:
    mean: list[float]
    components: list[list[float]]  # unit vectors, strongest first
    dimension: int


def compute_calibration(
    vectors: list[list[float]],
    *,
    components: int = 1,
    min_corpus: int = 16,
) -> Calibration | None:
    """Corpus mean + top principal components via power iteration.
    Deterministic (fixed start vector, fixed iteration count). Returns None
    when the corpus is too small or numpy is unavailable."""
    if len(vectors) < max(2, min_corpus):
        return None
    try:
        import numpy as np
    except ImportError:
        return None

    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] < 2:
        return None
    mean = matrix.mean(axis=0)
    centered = matrix - mean

    pcs: list[list[float]] = []
    residual = centered
    dim = matrix.shape[1]
    for _ in range(max(0, int(components))):
        # Deterministic start: alternating unit pattern, never the zero vector.
        vec = np.ones(dim, dtype=np.float64)
        vec[1::2] = -1.0
        vec /= np.linalg.norm(vec)
        for _ in range(_POWER_ITERATIONS):
            vec = residual.T @ (residual @ vec)
            norm = np.linalg.norm(vec)
            if norm < 1e-12:
                vec = None  # type: ignore[assignment]
                break
            vec /= norm
        if vec is None:
            break
        pcs.append([float(x) for x in vec])
        # Deflate: remove this component before finding the next.
        residual = residual - np.outer(residual @ vec, vec)

    if not pcs:
        return None
    return Calibration(mean=[float(x) for x in mean], components=pcs, dimension=dim)


def apply_correction(vector: list[float], calibration: Calibration) -> list[float]:
    """Correct one vector into the calibrated space. Pure Python — cheap at
    query time; the stored corpus is corrected once per index load."""
    if len(vector) != calibration.dimension:
        return vector
    corrected = [v - m for v, m in zip(vector, calibration.mean)]
    for pc in calibration.components:
        projection = sum(c * p for c, p in zip(corrected, pc))
        corrected = [c - projection * p for c, p in zip(corrected, pc)]
    norm = sum(c * c for c in corrected) ** 0.5
    if norm < 1e-12:
        return vector
    return [c / norm for c in corrected]
