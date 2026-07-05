from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_obsidian_common"))

from lisan_obsidian import NOT_FOUND, find_vault, safe_note_path  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    relative = str(args.get("path") or "").strip()
    if not relative:
        return "Error: path is required"
    max_chars = max(int(args.get("max_chars") or 10000), 200)
    obsidian_vault = find_vault(config)
    if obsidian_vault is None:
        return f"Error: {NOT_FOUND}"
    note = safe_note_path(obsidian_vault, relative)
    if note is None:
        return (
            f"Error: {relative!r} does not exist inside the vault "
            f"({obsidian_vault.name}) — paths must be relative to the vault root."
        )
    try:
        text = note.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Error: could not read note: {exc}"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… [note truncated at {max_chars} characters]"
    return f"# {note.stem}\n(path: {note.relative_to(obsidian_vault)})\n\n{text}"
