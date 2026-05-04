from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown


def parse_scalar(value: str) -> Any:
    text = value.strip()
    if text.lower() == "null":
        return None
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def edit_record(
    path: Path,
    set_fields: list[str] | None = None,
    add_fields: list[str] | None = None,
    append_body: str | None = None,
) -> Path:
    doc = load_markdown(path)
    frontmatter = dict(doc.frontmatter)
    file_type = str(frontmatter.get("type", ""))

    if "evidence/artifacts" in path.as_posix():
        raise ValueError("Evidence artifacts are immutable")

    if file_type == "evidence":
        if set_fields or add_fields:
            raise ValueError("Evidence metadata must be append-corrected in a separate correction file")

    if file_type == "episode":
        allowed = {
            "updated",
            "status",
            "review_after",
            "confidence",
            "confidence_basis",
            "last_confirmed",
            "significance_rationale",
            "summary",
            "links",
            "entities",
            "evidence",
            "claims",
            "arena_secondary",
        }
        for item in set_fields or []:
            key, _ = _split_assignment(item)
            if key not in allowed:
                raise ValueError(f"Episodes may only update append-safe fields; got {key}")
        if add_fields:
            raise ValueError("Episodes do not support list append edits; rewrite the record as an addendum instead")

    if file_type == "state":
        pass
    elif file_type in {"entity", "knowledge", "decision", "open_loop"}:
        pass
    elif file_type and file_type not in {"state", "entity", "knowledge", "decision", "open_loop", "episode", "evidence"}:
        raise ValueError(f"Unsupported record type: {file_type}")

    for item in set_fields or []:
        key, value = _split_assignment(item)
        frontmatter[key] = parse_scalar(value)

    for item in add_fields or []:
        key, value = _split_assignment(item)
        current = frontmatter.get(key, [])
        if not isinstance(current, list):
            raise ValueError(f"Field {key} is not a list")
        current.append(parse_scalar(value))
        frontmatter[key] = current

    body = doc.body.rstrip()
    if append_body:
        body = (body + "\n\n" + append_body.strip()).strip() + "\n"

    write_markdown(path, frontmatter, body)
    return path


def _split_assignment(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise ValueError(f"Expected key=value, got: {item}")
    key, value = item.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"Empty field name in assignment: {item}")
    return key, value
