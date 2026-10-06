"""Content instruction-risk labels, separate from authority/source tier."""
from __future__ import annotations

from typing import Any

TRUSTED = "trusted"
UNTRUSTED = "untrusted"
UNKNOWN = "unknown"


def normalize_content_trust(value: Any, *, default: str = UNKNOWN) -> str:
    label = str(value or default).strip().lower()
    if label in {TRUSTED, UNTRUSTED, UNKNOWN}:
        return label
    return UNKNOWN


def raise_content_trust(current: Any, incoming: Any) -> str:
    """Combine labels without allowing untrusted content to be laundered."""
    labels = {normalize_content_trust(current), normalize_content_trust(incoming)}
    if UNTRUSTED in labels:
        return UNTRUSTED
    if UNKNOWN in labels:
        return UNKNOWN
    return TRUSTED
