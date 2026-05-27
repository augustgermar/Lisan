"""Operating-style preference parser.

Reads ``primer/operating-style.md`` and returns a dict of recognized hard
preferences. Supports two formats:

  1. **JSON frontmatter** (preferred, new format — matches the rest of the
     vault). The file starts with a ``---`` block containing a JSON object::

         ---
         {
           "emotion-naming": false,
           "directness": true,
           "opener-style": "minimal",
           "summary-length": "short"
         }
         ---

         # Operating Style
         (free-text notes here for the LLM path)

  2. **Free-text fallback** (legacy). The parser does phrase matching on the
     body text for common preference statements like
     "doesn't want emotions named prematurely" or "prefer direct statements".

Defaults are safe and quiet: all preferences default to "no opinion" (i.e.
emotion-naming is allowed, no directness directive). Callers should treat
``None`` values as "use the agent's usual behavior."
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown


_DEFAULTS: dict[str, Any] = {
    "emotion-naming": None,   # True allows naming; False forbids; None = no opinion
    "directness": None,       # True = prefer direct statements
    "opener-style": None,     # "minimal" | "warm" | "neutral" | None
    "summary-length": None,   # "short" | "medium" | "long" | None
}


# Phrases that flip emotion-naming off in free-text primers.
_EMOTION_NAMING_OFF_PHRASES = (
    "don't name emotion",
    "do not name emotion",
    "no emotion naming",
    "doesn't want emotions named",
    "does not want emotions named",
    "doesn't want feelings named",
    "no naming feelings",
    "emotions not named",
    "no emotion-naming",
    "don't label emotions",
)

# Phrases that flip directness on.
_DIRECTNESS_ON_PHRASES = (
    "prefer direct",
    "value directness",
    "be direct",
    "direct and brief",
    "direct and concise",
    "directness valued",
    "appreciates directness",
    "values directness",
)

# Phrases that suggest minimal opener style.
_MINIMAL_OPENER_PHRASES = (
    "no preamble",
    "skip preamble",
    "minimal opener",
    "no small talk",
    "skip small talk",
)

# Phrases that suggest short summaries.
_SHORT_SUMMARY_PHRASES = (
    "short summaries",
    "keep it short",
    "brief responses",
    "be brief",
    "terse",
)


@lru_cache(maxsize=8)
def _load_cached(vault: Path, mtime: float) -> dict[str, Any]:
    path = vault / "primer" / "operating-style.md"
    if not path.exists():
        return dict(_DEFAULTS)
    try:
        doc = load_markdown(path)
    except Exception:
        return dict(_DEFAULTS)

    style = dict(_DEFAULTS)

    # Path 1: YAML frontmatter. The frontmatter parser returns a dict.
    fm = doc.frontmatter if isinstance(doc.frontmatter, dict) else {}
    for key in _DEFAULTS:
        if key in fm:
            style[key] = fm[key]

    # Path 2: phrase matching against the body (and against the raw file text
    # for legacy primers that lack frontmatter). Only fills in keys still at
    # their default of None — explicit frontmatter wins.
    body_text = (doc.body or "").lower()
    if style["emotion-naming"] is None:
        if any(p in body_text for p in _EMOTION_NAMING_OFF_PHRASES):
            style["emotion-naming"] = False
    if style["directness"] is None:
        if any(p in body_text for p in _DIRECTNESS_ON_PHRASES):
            style["directness"] = True
    if style["opener-style"] is None:
        if any(p in body_text for p in _MINIMAL_OPENER_PHRASES):
            style["opener-style"] = "minimal"
    if style["summary-length"] is None:
        if any(p in body_text for p in _SHORT_SUMMARY_PHRASES):
            style["summary-length"] = "short"

    return style


def load_operating_style(vault: Path) -> dict[str, Any]:
    """Return the parsed operating-style preferences for ``vault``.

    Result keys: ``emotion-naming``, ``directness``, ``opener-style``,
    ``summary-length``. Values are ``True``/``False``/string for explicit
    settings, or ``None`` for "no opinion / use defaults."
    """
    path = vault / "primer" / "operating-style.md"
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    return dict(_load_cached(vault, mtime))


def emotion_naming_allowed(vault: Path) -> bool:
    """Convenience: True iff the primer does not explicitly forbid naming emotions."""
    return load_operating_style(vault).get("emotion-naming") is not False


def prefers_directness(vault: Path) -> bool:
    """Convenience: True iff the primer explicitly asks for directness."""
    return load_operating_style(vault).get("directness") is True
