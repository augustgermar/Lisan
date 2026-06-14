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


# ---------------------------------------------------------------------------
# Structured principal source-of-truth: primer/identity-core.md
#
# Unlike the rest of the vault (JSON frontmatter parsed by ``frontmatter.py``),
# the identity core uses a small YAML block so it stays comfortable to hand-edit
# and reads as the spec authored it. The project has no YAML dependency, so this
# module ships a deliberately narrow parser that understands only the shape the
# core file uses: top-level scalars, one level of nested mapping, inline flow
# lists (``["a", "b"]``), and ``|`` literal blocks. It is NOT a general YAML
# parser and is intentionally confined to this one file.
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_scalar(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_unquote(item) for item in inner.split(",") if item.strip()]
    return _unquote(value)


def _extract_frontmatter(text: str) -> str | None:
    """Return the raw text between the first two ``---`` fences, or None."""
    if not text.lstrip().startswith("---"):
        return None
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == "---":
            start = idx
            break
    if start is None:
        return None
    for idx in range(start + 1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[start + 1 : idx])
    return None


def _parse_core_yaml(text: str) -> dict:
    """Parse the narrow YAML subset used by identity-core.md (see module note)."""
    data: dict = {}
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[: len(line) - len(line.lstrip())]:  # indented => not a top-level key
            i += 1
            continue
        key, _, raw = line.strip().partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw in {"|", "|-", ">"}:  # literal/folded block: gather indented lines
            i += 1
            block: list[str] = []
            while i < n:
                bl = lines[i]
                if bl.strip() and len(bl) - len(bl.lstrip()) == 0:
                    break
                block.append(bl)
                i += 1
            indents = [len(b) - len(b.lstrip()) for b in block if b.strip()]
            base = min(indents) if indents else 0
            data[key] = "\n".join(b[base:] if len(b) >= base else b for b in block).strip("\n")
            continue
        if raw == "":  # nested mapping: gather indented child scalars
            i += 1
            child: dict = {}
            while i < n:
                cl = lines[i]
                if not cl.strip() or cl.lstrip().startswith("#"):
                    i += 1
                    continue
                if len(cl) - len(cl.lstrip()) == 0:
                    break
                ckey, _, cval = cl.strip().partition(":")
                child[ckey.strip()] = _parse_scalar(cval)
                i += 1
            data[key] = child
            continue
        data[key] = _parse_scalar(raw)
        i += 1
    return data


@lru_cache(maxsize=8)
def _identity_core_cached(vault: Path, mtime: float) -> dict:
    """Cached parse of identity-core.md. ``mtime`` keys the cache so edits invalidate it."""
    path = vault / "primer" / "identity-core.md"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    fm = _extract_frontmatter(text)
    if fm is None:
        return {}
    try:
        return _parse_core_yaml(fm)
    except Exception:
        return {}


def _identity_core(vault: Path) -> dict:
    path = vault / "primer" / "identity-core.md"
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    return _identity_core_cached(vault, mtime)


def _fallback_principal_name(vault: Path) -> str | None:
    """Recover the principal's given name from the legacy ``You are {name}.`` line."""
    path = vault / "primer" / "identity.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    match = re.search(r"\bYou are\s+([A-Z][a-zA-Z]+)", text)
    return match.group(1) if match else None


def principal_aliases(vault: Path) -> frozenset[str]:
    """Name tokens that mean "you" (the principal). Drives owner/deixis resolution.

    Sourced from ``identity-core.md`` (principal.aliases + the given-name token of
    principal.name). Falls back to the ``You are {name}.`` line in ``identity.md``
    so vaults predating the core file still resolve a principal. Empty set means
    "no principal known" — callers must not treat an arbitrary name as the user.
    """
    core = _identity_core(vault)
    result: set[str] = set()
    principal = core.get("principal") if isinstance(core, dict) else None
    if isinstance(principal, dict):
        for alias in principal.get("aliases") or []:
            if alias and str(alias).strip():
                result.add(str(alias).strip())
        name = str(principal.get("name") or "").strip()
        if name:
            result.add(name.split()[0])
    if result:
        return frozenset(result)
    fallback = _fallback_principal_name(vault)
    return frozenset({fallback}) if fallback else frozenset()


def principal_display_name(vault: Path) -> str:
    """The principal's primary display name, e.g. ``August``."""
    core = _identity_core(vault)
    principal = core.get("principal") if isinstance(core, dict) else None
    if isinstance(principal, dict):
        aliases = principal.get("aliases") or []
        if aliases:
            return str(aliases[0])
        name = str(principal.get("name") or "").strip()
        if name:
            return name.split()[0]
    fallback = _fallback_principal_name(vault)
    return fallback or "the user"


def assistant_name(vault: Path) -> str:
    """The assistant's name (default ``Lisan``)."""
    core = _identity_core(vault)
    assistant = core.get("assistant") if isinstance(core, dict) else None
    if isinstance(assistant, dict):
        if assistant.get("name"):
            return str(assistant["name"]).strip()
        aliases = assistant.get("aliases") or []
        if aliases:
            return str(aliases[0]).strip()
    return "Lisan"


def assistant_aliases(vault: Path) -> frozenset[str]:
    """Name tokens that mean the assistant. Defaults to ``{assistant_name}``."""
    core = _identity_core(vault)
    assistant = core.get("assistant") if isinstance(core, dict) else None
    result: set[str] = set()
    if isinstance(assistant, dict):
        for alias in assistant.get("aliases") or []:
            if alias and str(alias).strip():
                result.add(str(alias).strip())
        if assistant.get("name"):
            result.add(str(assistant["name"]).strip())
    if result:
        return frozenset(result)
    return frozenset({assistant_name(vault)})


def deixis_frame(vault: Path) -> str:
    """The prose deixis frame from identity-core.md, or a name-free fallback."""
    core = _identity_core(vault)
    frame = core.get("deixis_frame") if isinstance(core, dict) else None
    if frame and str(frame).strip():
        return str(frame).strip()
    name = principal_display_name(vault)
    who = name if name and name != "the user" else "the principal"
    return (
        "I / me / Lisan = the assistant (software; no body, no family of its own).\n"
        f"you / your = {who}, the principal. Every stored record describes you.\n"
        "all other names = third parties; refer to them by name."
    )
