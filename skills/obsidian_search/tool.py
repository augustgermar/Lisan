from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_obsidian_common"))

from lisan_obsidian import NOT_FOUND, find_vault, iter_notes  # noqa: E402

MAX_FILE_BYTES = 2 * 1024 * 1024
SNIPPETS_PER_NOTE = 3


def search_vault(vault: Path, query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.lower()
    hits: list[dict[str, Any]] = []
    for note in iter_notes(vault):
        title_hit = needle in note.stem.lower()
        snippets: list[str] = []
        count = 0
        try:
            if note.stat().st_size <= MAX_FILE_BYTES:
                for lineno, line in enumerate(
                    note.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if needle in line.lower():
                        count += 1
                        if len(snippets) < SNIPPETS_PER_NOTE:
                            snippets.append(f"L{lineno}: {line.strip()[:200]}")
        except OSError:
            continue
        if title_hit or count:
            hits.append(
                {
                    "path": str(note.relative_to(vault)),
                    "title": note.stem,
                    "matches": count + (1 if title_hit else 0),
                    "snippets": snippets,
                }
            )
    hits.sort(key=lambda h: -h["matches"])
    return hits[:limit]


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: query is required"
    limit = min(max(int(args.get("limit") or 10), 1), 30)
    obsidian_vault = find_vault(config)
    if obsidian_vault is None:
        return f"Error: {NOT_FOUND}"
    results = search_vault(obsidian_vault, query, limit)
    if not results:
        return f"No notes matched {query!r} in {obsidian_vault.name}."
    return json.dumps(
        {"vault": str(obsidian_vault), "results": results},
        indent=2,
        ensure_ascii=False,
    )
