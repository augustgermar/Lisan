"""Anisotropy correction: compressed cosine cones get their spread back,
calibration is deterministic, small corpora and hash spaces are left alone,
and the query is corrected into the same space as the corpus."""
from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from lisan.tools.anisotropy import Calibration, apply_correction, compute_calibration
from lisan.tools.vector_store import build_query_scorer, clear_index_cache, cosine_similarity, load_index


def _cone_corpus(n: int = 40, dim: int = 16, seed: int = 7) -> list[list[float]]:
    """Vectors sharing one dominant direction plus small distinct signal —
    the anisotropic cone: raw cosines cluster near 1."""
    rng = random.Random(seed)
    vectors = []
    for _ in range(n):
        vec = [25.0] + [rng.gauss(0.0, 1.0) for _ in range(dim - 1)]
        norm = math.sqrt(sum(x * x for x in vec))
        vectors.append([x / norm for x in vec])
    return vectors


class CalibrationTests(unittest.TestCase):
    def test_correction_restores_spread(self) -> None:
        corpus = _cone_corpus()
        raw_pairs = [cosine_similarity(corpus[i], corpus[i + 1]) for i in range(0, 20, 2)]
        self.assertGreater(min(raw_pairs), 0.9)  # the cone: everything looks similar

        calibration = compute_calibration(corpus, components=1, min_corpus=8)
        self.assertIsNotNone(calibration)
        corrected = [apply_correction(v, calibration) for v in corpus]
        corrected_pairs = [cosine_similarity(corrected[i], corrected[i + 1]) for i in range(0, 20, 2)]
        self.assertLess(max(corrected_pairs), 0.6)  # spread restored

    def test_calibration_is_deterministic(self) -> None:
        corpus = _cone_corpus()
        a = compute_calibration(corpus, components=2, min_corpus=8)
        b = compute_calibration(corpus, components=2, min_corpus=8)
        self.assertEqual(a.mean, b.mean)
        self.assertEqual(a.components, b.components)

    def test_small_corpus_is_left_alone(self) -> None:
        self.assertIsNone(compute_calibration(_cone_corpus(n=5), min_corpus=16))

    def test_corrected_vectors_are_unit_norm(self) -> None:
        corpus = _cone_corpus()
        calibration = compute_calibration(corpus, min_corpus=8)
        for vec in corpus[:5]:
            corrected = apply_correction(vec, calibration)
            self.assertAlmostEqual(sum(x * x for x in corrected), 1.0, places=6)

    def test_dimension_mismatch_is_a_noop(self) -> None:
        calibration = Calibration(mean=[0.0, 0.0], components=[[1.0, 0.0]], dimension=2)
        self.assertEqual(apply_correction([1.0, 2.0, 3.0], calibration), [1.0, 2.0, 3.0])


class IndexIntegrationTests(unittest.TestCase):
    def _write_index(self, path: Path, vectors: list[list[float]], model: str) -> None:
        lines = [json.dumps({"__meta__": {"model": model, "dimension": len(vectors[0]), "version": 1}})]
        for i, vec in enumerate(vectors):
            lines.append(json.dumps({"id": f"rec-{i}", "embedding": vec}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _config(self, **anisotropy):
        return {"retrieval": {"embeddings": {"mode": "hash", "anisotropy": {"min_corpus": 8, **anisotropy}}}}

    def test_semantic_index_is_calibrated_on_load(self) -> None:
        clear_index_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            corpus = _cone_corpus()
            self._write_index(path, corpus, model="BAAI/bge-small-en-v1.5")
            index = load_index(path, config=self._config(enabled=True))
            self.assertIsNotNone(index.calibration)
            scoring = index.vectors_for_scoring()
            pair = cosine_similarity(scoring["rec-0"], scoring["rec-1"])
            self.assertLess(pair, 0.6)
            # Persistence safety: .vectors stays RAW — anything writing the
            # index back to disk must never see corrected vectors.
            self.assertEqual(index.vectors["rec-0"], corpus[0])

    def test_hash_index_is_never_calibrated(self) -> None:
        clear_index_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            self._write_index(path, _cone_corpus(), model="hash-fallback")
            index = load_index(path, config=self._config(enabled=True))
            self.assertIsNone(index.calibration)

    def test_disabled_leaves_raw_space(self) -> None:
        clear_index_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            self._write_index(path, _cone_corpus(), model="BAAI/bge-small-en-v1.5")
            index = load_index(path, config=self._config(enabled=False))
            self.assertIsNone(index.calibration)
            self.assertIsNone(index.scoring_vectors)
            self.assertGreater(cosine_similarity(index.vectors["rec-0"], index.vectors["rec-1"]), 0.9)

    def test_query_scorer_corrects_query_into_same_space(self) -> None:
        clear_index_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            corpus = _cone_corpus()
            self._write_index(path, corpus, model="BAAI/bge-small-en-v1.5")

            class FakeProvider:
                def embed_query(self, query):
                    from types import SimpleNamespace

                    return SimpleNamespace(vector=list(corpus[0]), mode_used="semantic")

            scorer = build_query_scorer(
                "anything", embeddings_file=path, config=self._config(enabled=True), provider=FakeProvider()
            )
            # The query IS rec-0: in the corrected space it must still match
            # itself perfectly, and beat its neighbors decisively.
            self.assertAlmostEqual(scorer.score("rec-0"), 1.0, places=5)
            self.assertLess(scorer.score("rec-1"), 0.6)


if __name__ == "__main__":
    unittest.main()
