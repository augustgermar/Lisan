from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MarkdownDocument:
    frontmatter: dict[str, Any]
    body: str


class FrontmatterError(ValueError):
    pass


def parse_markdown(text: str) -> MarkdownDocument:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return MarkdownDocument(frontmatter={}, body=text)

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("Frontmatter must start with ---")

    closing_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_index = i
            break
    if closing_index is None:
        raise FrontmatterError("Frontmatter closing --- not found")

    raw = "\n".join(lines[1:closing_index]).strip()
    body = "\n".join(lines[closing_index + 1 :]).lstrip("\n")
    if not raw:
        frontmatter: dict[str, Any] = {}
    else:
        try:
            frontmatter = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FrontmatterError(f"Frontmatter must be valid JSON: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise FrontmatterError("Frontmatter must decode to an object")
    return MarkdownDocument(frontmatter=frontmatter, body=body)


def load_markdown(path: Path) -> MarkdownDocument:
    return parse_markdown(path.read_text(encoding="utf-8"))


def dump_markdown(frontmatter: dict[str, Any], body: str) -> str:
    frontmatter_text = json.dumps(frontmatter, indent=2, sort_keys=False, ensure_ascii=True)
    body = body.rstrip()
    if body:
        return f"---\n{frontmatter_text}\n---\n\n{body}\n"
    return f"---\n{frontmatter_text}\n---\n"


def write_markdown(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    from .tools.kernel import guard_kernel_write

    guard_kernel_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_markdown(frontmatter, body), encoding="utf-8")

