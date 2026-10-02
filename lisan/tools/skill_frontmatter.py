"""Edit a SKILL.md's frontmatter without disturbing the rest of the file.

The learning loop must set provenance (`metadata:`), bump `version`, and — only
when a reviewer's patch touches it — change `description`. It must do so
without reflowing, reordering or losing anything else the owner wrote, so this
edits lines in place instead of re-serialising the parsed dictionary (which
would drop comments, key order and anything the tolerant parser did not model).

Values are written as plain scalars; `metadata` is the one-level block the
parser reads (see skill_format.parse_frontmatter).
"""
from __future__ import annotations

import re
from typing import Any

_FENCE = "---"


class FrontmatterError(ValueError):
    pass


def _split(text: str) -> tuple[list[str], list[str]]:
    """(frontmatter lines without fences, everything after the closing fence)."""
    if not text.startswith(_FENCE):
        raise FrontmatterError("no frontmatter to edit")
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() in (_FENCE, "..."):
            return lines[1:index], lines[index:]
    raise FrontmatterError("frontmatter is not closed")


def _join(front: list[str], rest: list[str]) -> str:
    return "\n".join([_FENCE, *front, *rest])


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    text = str(value)
    if "\n" in text:
        raise FrontmatterError("frontmatter values must be a single line")
    return text


def set_field(text: str, key: str, value: Any) -> str:
    """Set (or add) a top-level scalar field, keeping its position if present."""
    front, rest = _split(text)
    pattern = re.compile(rf"^{re.escape(key)}\s*:")
    line = f"{key}: {_scalar(value)}"
    for index, existing in enumerate(front):
        if pattern.match(existing):
            front[index] = line
            # a multi-line value (block list) would leave its items behind
            end = index + 1
            while end < len(front) and front[end][:1].isspace():
                del front[end]
            return _join(front, rest)
    front.append(line)
    return _join(front, rest)


def set_metadata(text: str, updates: dict[str, Any]) -> str:
    """Set keys inside the `metadata:` block, creating the block if needed.
    Other metadata keys and everything outside the block are left alone."""
    front, rest = _split(text)
    start = next((i for i, line in enumerate(front) if re.match(r"^metadata\s*:\s*$", line)), None)
    if start is None:
        front.append("metadata:")
        start = len(front) - 1
    end = start + 1
    while end < len(front) and front[end][:1].isspace():
        end += 1
    block = front[start + 1:end]
    for key, value in updates.items():
        line = f"  {key}: {_scalar(value)}"
        for i, existing in enumerate(block):
            if re.match(rf"^\s+{re.escape(key)}\s*:", existing):
                block[i] = line
                break
        else:
            block.append(line)
    return _join(front[: start + 1] + block + front[end:], rest)


_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def bump_patch_version(text: str, default: str = "0.1.0") -> str:
    """x.y.z -> x.y.(z+1); a missing or non-semver version starts at `default`."""
    from .skill_format import parse_frontmatter

    data, _ = parse_frontmatter(text)
    current = str(data.get("version") or "")
    match = _VERSION.match(current)
    new = f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}" if match else default
    return set_field(text, "version", new)
