from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
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


def listify(value: Any) -> list[str]:
    """Coerce a value into a list of non-empty strings."""
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


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



def utc_now_iso() -> str:
    """Current UTC time as a second-precision ISO string with a Z suffix —
    the canonical timestamp format for every stored record and job row."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: str) -> datetime | None:
    """Parse an ISO timestamp (Z or offset form) into an aware UTC datetime;
    naive inputs are assumed UTC. Returns None rather than raising."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def json_dumps_stable(value: Any) -> str:
    """Deterministic JSON for stored payloads: sorted keys, indented, ASCII."""
    return json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True)


def json_loads_forgiving(value: Any) -> Any:
    """Best-effort JSON load for values coming back out of storage: None and
    empty become None, already-parsed containers pass through, and text that
    isn't valid JSON is returned as-is rather than raising."""
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
