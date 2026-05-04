from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import vault_root, sqlite_path
from ..tools.common import iter_markdown_files


def assemble_context(query: str, arena: str | None = None, vault: Path | None = None, db_path: Path | None = None) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    sections: list[str] = ["# Assembled Context", ""]
    if arena:
        sections.append(f"arena: {arena}")
        sections.append("")
    sections.append(f"query: {query}")
    sections.append("")

    for rel in ["primer/identity.md", "primer/operating-style.md", "primer/current-brief.md"]:
        path = vault / rel
        if path.exists():
            sections.append(f"## {rel}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    if arena:
        sections.append("## State")
        for path in sorted((vault / "state").glob(f"*{arena}*")):
            sections.append(f"### {path.name}")
            sections.append(path.read_text(encoding="utf-8").strip())
            sections.append("")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sections.append("## Matching Records")
        try:
            rows = conn.execute(
                "SELECT id, type, summary, path FROM files WHERE summary LIKE ? OR id LIKE ? ORDER BY updated DESC LIMIT 10",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        if rows:
            for row in rows:
                sections.append(f"- `{row['id']}` | {row['type']} | {row['summary']} | `{row['path']}`")
        else:
            sections.append("- None")
    finally:
        conn.close()

    return "\n".join(sections).rstrip() + "\n"

