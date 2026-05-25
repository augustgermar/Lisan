from __future__ import annotations

from typing import Any


def normalize_domain_fields(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    domain_primary = data.get("domain_primary", data.get("arena_primary"))
    domain_secondary = data.get("domain_secondary", data.get("arena_secondary"))
    if domain_primary is not None and "domain_primary" not in data:
        data["domain_primary"] = domain_primary
    if domain_secondary is not None and "domain_secondary" not in data:
        data["domain_secondary"] = domain_secondary
    if domain_primary is not None and "arena_primary" not in data:
        data["arena_primary"] = domain_primary
    if domain_secondary is not None and "arena_secondary" not in data:
        data["arena_secondary"] = domain_secondary
    return data


def domain_primary(frontmatter: dict[str, Any], default: str = "cross_arena") -> str:
    value = frontmatter.get("domain_primary", frontmatter.get("arena_primary", default))
    return str(value or default)


def domain_secondary(frontmatter: dict[str, Any]) -> list[str]:
    value = frontmatter.get("domain_secondary", frontmatter.get("arena_secondary", []))
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def with_domain_fields(frontmatter: dict[str, Any]) -> dict[str, Any]:
    data = dict(frontmatter)
    primary = data.get("domain_primary", data.get("arena_primary"))
    secondary = data.get("domain_secondary", data.get("arena_secondary"))
    if primary is not None:
        data["domain_primary"] = primary
        data["arena_primary"] = primary
    if secondary is not None:
        data["domain_secondary"] = secondary
        data["arena_secondary"] = secondary
    return data
