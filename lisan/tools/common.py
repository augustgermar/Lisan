from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import MarkdownDocument, load_markdown


@dataclass(slots=True)
class VaultFile:
    path: Path
    doc: MarkdownDocument

    @property
    def frontmatter(self) -> dict[str, Any]:
        return self.doc.frontmatter

    @property
    def body(self) -> str:
        return self.doc.body

    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", ""))


def iter_markdown_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.md") if ".git" not in p.parts and "Draft5.md" not in p.name
    )


def read_vault_file(path: Path) -> VaultFile:
    return VaultFile(path=path, doc=load_markdown(path))


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return None
