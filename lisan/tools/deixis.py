# lisan/tools/deixis.py
from __future__ import annotations
import re
from pathlib import Path
from typing import Literal
from .primer_index import principal_aliases, assistant_aliases

Audience = Literal["interlocutor", "substrate", "display"]
# interlocutor -> conscious surface: {{principal}}->"you", {{self}}->"I"
# substrate    -> writer/skeptic/dreamer world-model: {{principal}}->"the user", {{self}}->"Lisan"
# display      -> human view (health, listings, Obsidian): {{principal}}->principal name, {{self}}->"Lisan"
#
# {{principal}} is the canonical token (it names the role, not the pronoun, which
# is what lets the `audience` seam re-address it for the C-3PO trajectory).
# {{user}} is accepted only as a legacy back-compat synonym.

_PRINCIPAL_TOK = re.compile(r"\{\{\s*(?:principal|user)\s*\}\}")
_SELF_TOK = re.compile(r"\{\{\s*self\s*\}\}")
_UNRESOLVED_TOK = re.compile(r"\{\{\s*[^{}]+\s*\}\}")


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
        u, s = "the user", "Lisan"
    else:  # display
        if principal_name:
            u = principal_name
        elif vault is not None:
            names = sorted(principal_aliases(vault), key=len, reverse=True)
            u = names[0] if names else "the user"
        else:
            u = "the user"
        s = "Lisan"
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


def render_for_display(text: str, vault: Path) -> str:
    """Render tokens for human-facing output (reports, Obsidian): {{principal}} -> name."""
    from .primer_index import principal_display_name
    return render_deixis(text, "display", principal_name=principal_display_name(vault))


def has_unresolved_token(text: str) -> bool:
    """Return True when text still contains a raw role token."""
    return bool(text and _UNRESOLVED_TOK.search(text))
