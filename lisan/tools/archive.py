from __future__ import annotations

from pathlib import Path

from ..frontmatter import load_markdown, write_markdown


def archive_open_loop(path: Path, force: bool = False) -> Path:
    doc = load_markdown(path)
    frontmatter = dict(doc.frontmatter)
    if str(frontmatter.get("type")) != "open_loop":
        raise ValueError("Only open_loop records can be archived with this command")
    if not force and str(frontmatter.get("status")) not in {"resolved", "archived"}:
        raise ValueError("Open loops should be resolved before archiving; use --force to override")

    frontmatter["id"] = f"{frontmatter.get('id', path.stem)}.archived"
    frontmatter["status"] = "archived"
    target = path.parents[1] / "archive" / "open_loops" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(target, frontmatter, doc.body)
    path.unlink()
    return target
