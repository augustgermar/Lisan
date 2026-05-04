from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> Any | None:
    candidates: list[str] = []

    # 1. Fenced code blocks (```json ... ``` or ``` ... ```)
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE):
        candidates.append(m.group(1).strip())

    # 2. Raw text as-is
    candidates.append(text.strip())

    # 3. First {...} or [...] spanning balanced braces (handles preamble text)
    bracket_match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if bracket_match:
        candidates.append(bracket_match.group(1).strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 4. Last-resort: trim any leading/trailing non-JSON prose and retry
    stripped = text.strip()
    start = max(stripped.find("{"), stripped.find("["))
    if start > 0:
        end = max(stripped.rfind("}"), stripped.rfind("]")) + 1
        if end > start:
            try:
                return json.loads(stripped[start:end])
            except json.JSONDecodeError:
                pass

    return None


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
