"""Scope: the delegation axis. What the owner is *responsible for*.

Deliberately not the same thing as ``domain`` (the life-dimension axis:
``physical``, ``relational``, ``work``, ``cross_arena``, ...). Domain is a
fixed, closed enum built for memory retrieval and the psychological
self-model. Scope is free text the owner defines, and it is the only axis
the Adjutant's authority gate reads.

Why they had to be split (WO-ADJUTANT, 2026-07-29). Delegation was
originally grafted onto the domain enum under the name ``arena``. The
questions that axis can answer are the wrong ones — *"may the agent write
files in the `relational` domain?"* is close to meaningless, while *"may
it act unattended on the greenhouse budget?"* is exactly how a person
delegates. So an owner writing an authority document reaches for areas of
responsibility, the schema had no field for them, and every rule they
wrote matched nothing:

- ``primer/intent.md`` declared eight areas of responsibility.
- Records carried life dimensions (``cross_arena`` on the large majority).
- The overlap was empty, so every task resolved through ``defaults`` and
  ``execute`` was unreachable by construction.

The failure was invisible because ``files.arena`` was populated as
``COALESCE(arena, domain_primary, arena_primary)`` — an absent delegation
scope silently became a life dimension, so the gate always had *something*
plausible to read and never reported a gap.

Two rules follow, and they are the whole design:

1. **Never fall back.** :func:`record_scope` reads one field. A record
   without a scope reports no scope, and the gate says so out loud. An
   absent authority declaration must be visible, not substituted.
2. **Only the owner assigns scope.** The capture pipeline never invents
   one — a model guessing at areas of responsibility is precisely how the
   original mismatch was manufactured. Scope arrives on records the owner
   created deliberately (``lisan new loop --scope``, a schedule record) or
   not at all.
"""
from __future__ import annotations

from typing import Any

# The canonical delegations key in intent.md. ``arenas`` is the legacy
# spelling and stays readable forever: an owner's adopted authority
# document must not be invalidated by a vocabulary change in the code.
SCOPES_KEY = "scopes"
LEGACY_SCOPES_KEY = "arenas"


def normalize_scope(value: Any) -> str:
    """Canonical form of a scope name: stripped and lowercased.

    Casing drift was a real observed failure (records carrying
    ``"Lisan System"`` beside ``system``), and a delegation that fails to
    match because of a capital letter is the worst kind of silent gap —
    it looks like a considered rule and behaves like no rule. Normalizing
    both sides at every boundary makes the class impossible rather than
    unlikely.
    """
    if value is None:
        return ""
    return str(value).strip().lower()


def record_scope(frontmatter: dict[str, Any]) -> str:
    """The delegation scope declared on a record, or ``""``.

    Reads ``scope`` and nothing else — see rule 1 in the module docstring.
    Resist the temptation to fall back to ``domain_primary`` here however
    convenient it looks; that single line of convenience is the entire bug
    this module exists to retire.
    """
    return normalize_scope(frontmatter.get("scope"))


def declared_scopes(delegations: dict[str, Any]) -> dict[str, Any]:
    """Scope rules from an intent delegations block, keys normalized.

    Accepts the canonical ``scopes`` and the legacy ``arenas``. When both
    are present, ``scopes`` wins per key — the validator separately flags
    the ambiguity so the owner is told rather than quietly overruled.
    """
    merged: dict[str, Any] = {}
    for key in (LEGACY_SCOPES_KEY, SCOPES_KEY):
        block = delegations.get(key)
        if isinstance(block, dict):
            for name, rules in block.items():
                merged[normalize_scope(name)] = rules
    return merged


def scopes_key_in_use(delegations: dict[str, Any]) -> str:
    """Which spelling this document uses — for messages the owner reads."""
    if isinstance(delegations.get(SCOPES_KEY), dict):
        return SCOPES_KEY
    if isinstance(delegations.get(LEGACY_SCOPES_KEY), dict):
        return LEGACY_SCOPES_KEY
    return SCOPES_KEY
