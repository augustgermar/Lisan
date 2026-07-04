from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .onboarding import _write_identity, _write_identity_core


def seed_eval_primer(
    vault: Path,
    *,
    principal_name: str,
    background: str = "",
    values: str = "",
    relationships: str = "",
    principal_aliases: list[str] | None = None,
    roster_entries: list[dict[str, Any]] | None = None,
) -> None:
    """Seed an eval persona primer with both identity.md and identity-core.md.

    This is harness-side support, not product onboarding. It deliberately reuses
    the same identity writers as onboarding so the eval's primer shape cannot
    drift from the live runtime, then augments identity-core.md with the roster
    block the eval needs for deterministic principal/entity resolution.
    """
    primer = vault / "primer"
    primer.mkdir(parents=True, exist_ok=True)

    _write_identity(
        primer / "identity.md",
        name=principal_name,
        background=background,
        values=values,
        relationships=relationships,
    )
    _write_identity_core(primer / "identity-core.md", name=principal_name)
    _augment_identity_core(
        primer / "identity-core.md",
        principal_aliases=principal_aliases,
        roster_entries=roster_entries or [],
    )


def _augment_identity_core(
    path: Path,
    *,
    principal_aliases: list[str] | None,
    roster_entries: list[dict[str, Any]],
) -> None:
    text = path.read_text(encoding="utf-8")
    aliases = [_clean_text(alias) for alias in (principal_aliases or []) if _clean_text(alias)]
    if aliases:
        alias_line = f"  aliases: {_flow_list(aliases)}"
        text = re.sub(r"(?m)^  aliases: \[.*\]$", alias_line, text, count=1)
    roster_block = _roster_block(roster_entries)
    if roster_block:
        text = text.replace("deixis_frame: |", roster_block + "\ndeixis_frame: |", 1)
    from .onboarding import _ceremony_write_kernel

    _ceremony_write_kernel(path, text)


def _roster_block(entries: list[dict[str, Any]]) -> str:
    cleaned: list[str] = []
    for entry in entries:
        name = _clean_text(entry.get("name"))
        kind = _clean_text(entry.get("kind")).lower()
        if not name or not kind:
            continue
        cleaned.append(f'  - name: "{_yaml_escape(name)}"')
        aliases = [_clean_text(alias) for alias in (entry.get("aliases") or []) if _clean_text(alias)]
        if aliases:
            cleaned.append(f"    aliases: {_flow_list(aliases)}")
        cleaned.append(f"    kind: {kind}")
    if not cleaned:
        return ""
    return "roster:\n" + "\n".join(cleaned) + "\n"


def _flow_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_yaml_escape(value)}"' for value in values) + "]"


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _clean_text(value: Any) -> str:
    return str(value or "").strip()
