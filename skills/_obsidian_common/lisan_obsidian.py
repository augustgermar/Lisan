"""Shared helper for Lisan obsidian skills: locate the user's Obsidian vault.

STRICTLY READ-ONLY by design. The Obsidian vault is the user's source
material — Lisan's hard write boundary (see execution_tools) already forbids
codex from writing there; these skills only ever open files for reading.

Vault resolution order:
1. ``LISAN_OBSIDIAN_VAULT`` environment variable
2. ``skills.obsidian.vault_path`` in config.json
3. Obsidian's own registry (``obsidian.json``): the vault marked open,
   otherwise the most recently used one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

NOT_FOUND = (
    "No Obsidian vault found. Set `skills.obsidian.vault_path` in config.json "
    "or the LISAN_OBSIDIAN_VAULT environment variable to the vault directory."
)

_REGISTRY_LOCATIONS = (
    Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json",
    Path.home() / ".config" / "obsidian" / "obsidian.json",
)


def find_vault(config: dict[str, Any] | None = None) -> Path | None:
    env = os.environ.get("LISAN_OBSIDIAN_VAULT")
    if env:
        path = Path(env).expanduser()
        return path if path.is_dir() else None
    if config:
        configured = (config.get("skills") or {}).get("obsidian", {}).get("vault_path")
        if configured:
            path = Path(str(configured)).expanduser()
            return path if path.is_dir() else None
    for registry in _REGISTRY_LOCATIONS:
        if not registry.exists():
            continue
        try:
            vaults = json.loads(registry.read_text(encoding="utf-8")).get("vaults", {})
        except Exception:
            continue
        candidates = [v for v in vaults.values() if isinstance(v, dict) and v.get("path")]
        if not candidates:
            continue
        chosen = next(
            (v for v in candidates if v.get("open")),
            max(candidates, key=lambda v: v.get("ts", 0)),
        )
        path = Path(str(chosen["path"])).expanduser()
        if path.is_dir():
            return path
    return None


def iter_notes(vault: Path):
    """All markdown notes, skipping Obsidian's own metadata and trash."""
    for path in sorted(vault.rglob("*.md")):
        if any(part in (".obsidian", ".trash") for part in path.parts):
            continue
        if path.is_file():
            yield path


def safe_note_path(vault: Path, relative: str) -> Path | None:
    """Resolve a note path inside the vault; None if it escapes (traversal
    or symlink) or does not exist."""
    candidate = (vault / relative).resolve()
    try:
        candidate.relative_to(vault.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        # Convenience: allow passing a note title without the .md suffix.
        with_suffix = candidate.with_suffix(".md")
        if with_suffix.is_file():
            try:
                with_suffix.relative_to(vault.resolve())
                return with_suffix
            except ValueError:
                return None
        return None
    return candidate
