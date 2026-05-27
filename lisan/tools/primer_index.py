"""Primer-derived 'known cast' index.

Used by the writer's entity-stub validator, the entity-merge tiebreaker, and the
elicitor's fallback to recognize people the user has actually told us about.

Parser is intentionally narrow: it extracts capitalized name-shaped tokens from
``primer/identity.md`` and registers both full names and individual name tokens.
A primer that uses a structured ``people:`` block (anticipated future schema)
can extend this without changing call sites.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


# One to three consecutive capitalized words. Allows hyphenated last names by
# permitting an internal hyphen-followed-by-capital pattern inside each token.
# Uses ``[ \t]+`` for the inter-word gap (single-line whitespace only) so the
# regex doesn't greedily span across newlines and pick up unrelated headings
# as part of a name.
_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:-[A-Z][a-z]+)?(?:[ \t]+[A-Z][a-z]+(?:-[A-Z][a-z]+)?){0,2})\b")


@lru_cache(maxsize=8)
def _known_names_cached(vault: Path, mtime: float) -> frozenset[str]:
    """Cached implementation. ``mtime`` keys the cache so edits invalidate it."""
    path = vault / "primer" / "identity.md"
    if not path.exists():
        return frozenset()
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return frozenset()
    names: set[str] = set()
    for match in _NAME_RE.finditer(text):
        value = match.group(1).strip()
        if len(value) < 3:
            continue
        names.add(value)
        for token in value.split():
            # Hyphenated halves are added as separate tokens too.
            for part in token.split("-"):
                if len(part) >= 3:
                    names.add(part)
            if len(token) >= 3:
                names.add(token)
    return frozenset(names)


def known_names(vault: Path) -> frozenset[str]:
    """Return the set of personal-name strings extracted from ``primer/identity.md``.

    The result includes both full names ("Marcus Webb") and individual tokens
    ("Marcus", "Webb") so a first-name-only mention of a primer person matches.

    Returns an empty set if the primer is missing — callers should treat that
    as "no allowlist available" and fall back to their own heuristics.
    """
    path = vault / "primer" / "identity.md"
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    return _known_names_cached(vault, mtime)


def name_in_text(name: str, text: str) -> bool:
    """Return True iff ``name`` appears as a standalone word in ``text``."""
    if not name or not text:
        return False
    pattern = r"\b" + re.escape(name) + r"\b"
    return re.search(pattern, text) is not None
