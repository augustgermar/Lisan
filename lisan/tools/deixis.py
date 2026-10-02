# lisan/tools/deixis.py
from __future__ import annotations
import re
from pathlib import Path
from typing import Literal
from .primer_index import assistant_display_name, assistant_aliases, principal_aliases

Audience = Literal["interlocutor", "substrate", "display"]
# interlocutor -> conscious surface: {{principal}}->"you", {{self}}->"I"
# substrate    -> writer/skeptic/dreamer world-model: {{principal}}->"the user", {{self}}->assistant display name
# display      -> human view (health, listings, Obsidian): {{principal}}->principal name, {{self}}->assistant display name
#
# {{principal}} is the canonical token (it names the role, not the pronoun, which
# is what lets the `audience` seam re-address it for the C-3PO trajectory).
# {{user}} is accepted only as a legacy back-compat synonym.

_PRINCIPAL_TOK = re.compile(r"\{\{\s*(?:principal|user)\s*\}\}")
_SELF_TOK = re.compile(r"\{\{\s*self\s*\}\}")
_UNRESOLVED_TOK = re.compile(r"\{\{\s*[^{}]+\s*\}\}")

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH = "(?:" + "|".join(_MONTH_NAMES) + ")"
_DAY = r"(?:0?[1-9]|[12][0-9]|3[01])(?:st|nd|rd|th)?"
_YEAR = r"(?:19|20)\d{2}"
_DATE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"\b{_MONTH}\s+{_DAY}(?:\s*,?\s*{_YEAR})?\b",
    rf"\b{_DAY}\s+{_MONTH}(?:\s*,?\s*{_YEAR})?\b",
    rf"\b{_MONTH}\s+{_YEAR}\b",
    rf"\b{_YEAR}[-/]{_MONTH}[-/]{_DAY}\b",
    rf"\b{_MONTH}[-/]{_DAY}[-/]{_YEAR}\b",
    rf"\b(?:in|during|throughout|since|until|by)\s+{_MONTH}\b",
    rf"\bbirthday(?:\s+is|\s+falls)?(?:\s+sometime)?\s+in\s+{_MONTH}\b",
    rf"\bsometime\s+in\s+{_MONTH}\b",
))
_QUOTED_PATTERNS = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"`[^`\n]+`"),
    re.compile(r'"[^"\n]*"'),
    re.compile(r"“[^”\n]*”"),
    re.compile(r"(?<!\w)'[^'\n]+'(?!\w)"),
    re.compile(r"^\s*>.*$", re.MULTILINE),
)
_LITERAL_FIELD_NAMES = frozenset({
    "name", "canonical_name",
    "id", "ids", "links",
    "source", "sources", "source_id", "source_ids", "source_path", "source_url",
    "provenance", "provenance_id", "provenance_ids",
    "quote", "quoted_text", "verbatim", "verbatim_excerpt", "excerpt",
    "raw", "raw_text", "source_text", "original_text", "transcript",
    "created", "updated", "observed_at", "valid_until", "review_after",
    "first_seen", "last_seen", "last_reviewed", "last_confirmed", "timestamp", "date",
})


def render_deixis(
    text: str,
    audience: Audience,
    vault: Path | None = None,
    *,
    principal_name: str | None = None,
) -> str:
    """Resolve role tokens to person for one audience.

    ``interlocutor`` and ``substrate`` are name-independent, so ``vault`` is
    optional and only consulted for ``display``. For ``display`` a caller may
    pass ``principal_name`` directly (the fast path used by ``render_for_display``);
    otherwise the principal's display name is resolved from ``vault``. With
    neither, ``display`` falls back to "the user".
    """
    if not text:
        return text
    if audience == "interlocutor":
        u, s = "you", "I"
    elif audience == "substrate":
        u, s = "the user", assistant_display_name(vault) if vault is not None else "Lisan"
    else:  # display
        if principal_name:
            u = principal_name
        elif vault is not None:
            names = sorted(principal_aliases(vault), key=len, reverse=True)
            u = names[0] if names else "the user"
        else:
            u = "the user"
        s = assistant_display_name(vault) if vault is not None else "Lisan"
    text = _PRINCIPAL_TOK.sub(u, text)
    text = _SELF_TOK.sub(s, text)
    return text


def render_obj(obj, audience: Audience, vault: Path | None = None, *, principal_name: str | None = None):
    """Recursively render tokens in str/list/dict structures (for narrative_state etc.)."""
    if isinstance(obj, str):
        return render_deixis(obj, audience, vault, principal_name=principal_name)
    if isinstance(obj, list):
        return [render_obj(x, audience, vault, principal_name=principal_name) for x in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v, audience, vault, principal_name=principal_name) for k, v in obj.items()}
    return obj


def tokenize_principal_obj(obj, vault: Path):
    """Recursively tokenize principal aliases in nested dict/list structures.

    Values under ``name`` and ``canonical_name`` keys are preserved literally so
    entity proper names stay stable on disk.
    """
    return _tokenize_principal_obj(obj, vault)


def render_for_display(text: str, vault: Path) -> str:
    """Render tokens for human-facing output (reports, Obsidian): {{principal}} -> name."""
    from .primer_index import principal_display_name
    return render_deixis(text, "display", principal_name=principal_display_name(vault))


def tokenize_principal(text: str, vault: Path) -> str:
    """Replace the principal's literal name with the {{principal}} token.

    Deterministic safety net for when a Writer model emits the principal's real
    name instead of the {{principal}} token the writer prompts request. Date
    expressions, quoted/verbatim text, and identifier-like occurrences are
    deliberately protected: a principal called August must not turn
    ``August 20, 2026`` into a role reference. Matches whole-word aliases (so a
    possessive like "Mara's" becomes "{{principal}}'s") and is idempotent.
    """
    if not text:
        return text
    protected = _protected_spans(text)
    for alias in sorted((a for a in principal_aliases(vault) if a), key=len, reverse=True):
        # ``\b`` considers hyphens, slashes, dots, and colons boundaries, which
        # made names inside IDs/paths date-like identifiers eligible.  These
        # explicit boundaries keep such machine strings literal.
        pattern = re.compile(rf"(?<![\w@./:#-]){re.escape(alias)}(?![\w@./:#-])")
        text = pattern.sub(
            lambda match: match.group(0) if _span_is_protected(match.span(), protected) else "{{principal}}",
            text,
        )
        # Earlier substitutions change offsets. Recompute spans before the
        # next alias rather than applying stale coordinates.
        protected = _protected_spans(text)
    return text


def _tokenize_principal_obj(obj, vault: Path, *, preserve_literal: bool = False):
    if isinstance(obj, str):
        return obj if preserve_literal else tokenize_principal(obj, vault)
    if isinstance(obj, list):
        return [_tokenize_principal_obj(item, vault, preserve_literal=preserve_literal) for item in obj]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            next_preserve = preserve_literal or _literal_field(str(key))
            out[key] = _tokenize_principal_obj(value, vault, preserve_literal=next_preserve)
        return out
    return obj


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in (*_DATE_PATTERNS, *_QUOTED_PATTERNS):
        spans.extend(match.span() for match in pattern.finditer(text))
    return spans


def _span_is_protected(span: tuple[int, int], protected: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start >= protected_start and end <= protected_end for protected_start, protected_end in protected)


def _literal_field(key: str) -> bool:
    """Does a structured field carry identity, source, or timestamp syntax?"""
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _LITERAL_FIELD_NAMES:
        return True
    if normalized.startswith(("source_", "provenance_", "raw_", "verbatim_", "quoted_")):
        return True
    return normalized.endswith(("_id", "_ids", "_path", "_url", "_hash", "_at", "_date", "_time"))


def has_unresolved_token(text: str) -> bool:
    """Return True when text still contains a raw role token."""
    return bool(text and _UNRESOLVED_TOK.search(text))
