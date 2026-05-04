from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .paths import repo_root


@lru_cache(maxsize=1)
def prompts_root(base: Path | None = None) -> Path:
    return (base or repo_root()) / "prompts"


def load_prompt(name: str, base: Path | None = None) -> str:
    path = prompts_root(base) / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def list_prompts(base: Path | None = None) -> list[str]:
    root = prompts_root(base)
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.md"))

