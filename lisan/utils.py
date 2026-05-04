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

