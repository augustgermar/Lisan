from __future__ import annotations

import hashlib
import re
from datetime import datetime, date
from pathlib import Path
from typing import Any


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "item"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def approx_word_count(text: str) -> int:
    return len(re.findall(r"\b\S+\b", text))


def approx_token_count(text: str) -> int:
    return max(1, round(approx_word_count(text) * 1.33))


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    return None


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return repr(value)


def path_rel(base: Path, target: Path) -> str:
    try:
        return str(target.relative_to(base))
    except ValueError:
        return str(target)


def hash_embedding(text: str, dimensions: int = 32) -> list[float]:
    """Deterministic, non-semantic hash embedding used as an explicit fallback.

    This is NOT a semantic embedding. Real semantic vectors come from
    ``lisan.providers.embeddings.EmbeddingProvider``; this fallback is used only
    when the embedding mode is ``hash`` or the embedder is unreachable and
    ``unreachable_policy`` is ``hash`` (mirroring the deterministic agent
    fallbacks in ``lisan/agents/base.py`` so the system still runs offline / in
    CI with no model server). Do not delete it."""
    import math
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [0.0] * dimensions
    for index, byte in enumerate(digest):
        vector[index % dimensions] += byte / 255.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]

