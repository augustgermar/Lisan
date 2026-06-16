"""Entity kind model (P3).

`kind` is a first-class, OPEN property of every entity. Assignment is
deterministic-first, mirroring the deixis/routing spine:

    roster (authoritative) -> structural signals (mechanical) -> model -> thing

`person` is NEVER a default at any layer — forced classification with no escape
hatch is what made "Atlas" (a project) and "Houston" (a city) become *people*.
`thing` is the honest fallback so a wrong guess degrades to vagueness, not a
confident lie. The kind set is open: unknown kinds are accepted, not rejected.
"""
from __future__ import annotations

import re
from pathlib import Path

# Canonical starter set (§3). OPEN — new kinds may appear in roster/config and
# must not break the system. This is the validation hint, not a closed enum.
CANONICAL_KINDS: frozenset[str] = frozenset({
    # animate / agentive
    "person", "pet", "agent", "organization",
    # concrete / physical
    "place", "system", "artifact",
    # abstract / conceptual
    "project", "event", "topic", "account",
    # fallback
    "thing",
})

FALLBACK_KIND = "thing"

# ── Layer 2: structural signals (high-precision, deterministic) ───────────────

_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?$")
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_URL = re.compile(r"^(https?|ssh|git)://", re.I)
_ORG_SUFFIX = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|corp|corp\.|ltd|ltd\.|co\.|gmbh|plc|"
    r"university|college|institute|foundation|agency|department|dept\.?|"
    r"bureau|court|guild|union|company)\b",
    re.I,
)
_ACCOUNT = re.compile(r"^(?:\$\d[\d,]*(?:\.\d+)?|\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}|[A-Z]{2}\d{2}[A-Z0-9]{10,})$")
_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_EVENT_WORDS = (
    "birthday", "party", "recital", "wedding", "funeral", "graduation",
    "appointment", "deadline", "deploy", "deployment", "outage", "incident",
    "practice", "rehearsal", "ceremony", "anniversary", "hearing", "trial date",
)


def classify_structural(token: str, context: str = "") -> str | None:
    """Mechanically classify a token by shape. Returns a kind, or None if no
    high-precision pattern fires (defer to the model). Never guesses `person`."""
    t = (token or "").strip()
    if not t:
        return None
    low = t.lower()
    if _IP.match(t):
        return "system"
    if _URL.match(t):
        return "system"
    if "/" in t or "\\" in t:
        # a path/repo ref: a file (has an extension) is an artifact; a dir/host
        # path is infrastructure.
        return "artifact" if _FILE_EXT.search(t) else "system"
    if _HOSTNAME.match(low) and (any(c.isdigit() for c in low) or "-" in low or low.rsplit(".", 1)[-1] in {"prod", "dev", "local", "test", "stage", "io", "com", "net", "org", "dev"}):
        return "system"
    if _ACCOUNT.match(t):
        return "account"
    if _ORG_SUFFIX.search(t):
        return "organization"
    haystack = f"{low} {context.lower()}"
    if any(w in haystack for w in _EVENT_WORDS):
        return "event"
    return None


# ── Three-layer assignment ────────────────────────────────────────────────────

def assign_kind(
    name: str,
    vault: Path,
    *,
    model_kind: str | None = None,
    summary: str = "",
    source_text: str = "",
) -> str:
    """Resolve an entity's kind: roster -> structural -> model -> thing.

    `person` is only ever produced by the roster or by an explicit model choice,
    never as a fallback. An unrecognized model kind is accepted (open set) only
    if it is non-empty and not a bare default; otherwise we fall to `thing`.
    """
    name = (name or "").strip()
    if not name:
        return FALLBACK_KIND

    # Layer 1 — roster (authoritative)
    from .primer_index import roster_kind
    rk = roster_kind(vault, name)
    if rk:
        return rk

    # Layer 2 — structural signals
    sk = classify_structural(name, context=f"{summary} {source_text}")
    if sk:
        return sk

    # Layer 3 — the model's explicit choice (allowed to be person, or anything
    # open). A blank/missing kind is NOT trusted — it falls through to `thing`.
    mk = (model_kind or "").strip().lower()
    if mk and mk != "unknown":
        return mk

    # Honest fallback — never `person`.
    return FALLBACK_KIND
