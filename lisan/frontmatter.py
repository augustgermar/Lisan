from __future__ import annotations

import json
import os
import sys
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


# str(path) -> (mtime_ns, size, frontmatter). Whole-vault passes (the
# write-boundary validator's reference index and duplicate-id scan, run once
# for the proposed record and once for the current one on every update)
# re-read every record in the vault per write: ~4.6 s per validation on the
# live vault, measured 2026-10-29. A file whose stat is unchanged is not
# read again. Callers get a deep copy, never the cached dict.
_FRONTMATTER_CACHE: dict[str, tuple[int, int, str]] = {}


def load_frontmatter_cached(path: Path) -> dict[str, Any]:
    """Frontmatter only, cached by (mtime_ns, size). Raises like load_markdown.

    The cache holds the serialized JSON, so every caller gets a fresh dict
    (C-speed json.loads) and can never mutate a shared copy."""
    stat = path.stat()
    key = str(path)
    cached = _FRONTMATTER_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return json.loads(cached[2])
    frontmatter = load_markdown(path).frontmatter
    _FRONTMATTER_CACHE[key] = (stat.st_mtime_ns, stat.st_size, json.dumps(frontmatter, ensure_ascii=False))
    return frontmatter


def dump_markdown(frontmatter: dict[str, Any], body: str) -> str:
    frontmatter_text = json.dumps(frontmatter, indent=2, sort_keys=False, ensure_ascii=True)
    body = body.rstrip()
    if body:
        return f"---\n{frontmatter_text}\n---\n\n{body}\n"
    return f"---\n{frontmatter_text}\n---\n"


def write_markdown(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    from .tools.kernel import guard_kernel_write
    from .tools.write_boundary import write_if_structured_record

    guard_kernel_write(path)
    rendered = dump_markdown(frontmatter, body)
    # Historical tests intentionally construct malformed records and then ask
    # the validator to diagnose them.  They need a raw fixture seam, but that
    # seam must be impossible in a running Lisan process.  The env flag alone
    # is insufficient: it is honored only while a test runner is actually
    # loaded.  Boundary-specific tests remove the flag and exercise production
    # behavior end to end.
    test_fixture_write = (
        os.environ.get("LISAN_TEST_RAW_RECORD_WRITES") == "1"
        and ("pytest" in sys.modules or "unittest" in sys.modules)
    )
    if not test_fixture_write and write_if_structured_record(path, frontmatter, body, rendered):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
