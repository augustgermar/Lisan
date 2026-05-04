from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import FrontmatterError, load_markdown
from ..paths import vault_root
from ..tools.common import iter_markdown_files, parse_date


def generate_manifests(vault: Path | None = None, write: bool = True) -> dict[str, str]:
    vault = vault or vault_root()
    records = []
    for path in iter_markdown_files(vault):
        if path.parts[-2] == "manifests" or path.parts[-2] == "transcripts":
            continue
        try:
            doc = load_markdown(path)
        except FrontmatterError:
            continue
        fm = doc.frontmatter
        if not fm:
            continue
        records.append(
            {
                "id": str(fm.get("id", "")),
                "type": str(fm.get("type", "")),
                "status": str(fm.get("status", "")),
                "significance": str(fm.get("significance", "")),
                "arena_primary": str(fm.get("arena_primary", "")),
                "summary": str(fm.get("summary", "")),
                "created": str(fm.get("created", "")),
                "updated": str(fm.get("updated", "")),
                "path": str(path.relative_to(vault)),
                "source": str(fm.get("source", "")),
            }
        )

    manifests = {
        "manifest-core.md": _build_core_manifest(records),
        "manifest-entities.md": _build_typed_manifest(records, {"entity"}),
        "manifest-knowledge.md": _build_typed_manifest(records, {"knowledge"}),
        "manifest-evidence.md": _build_typed_manifest(records, {"evidence"}),
        "manifest-decisions.md": _build_typed_manifest(records, {"decision"}),
        "manifest-open-loops.md": _build_typed_manifest(records, {"open_loop"}),
        "manifest-archive.md": _build_archive_manifest(records),
    }

    episode_groups = defaultdict(list)
    for record in records:
        if record["type"] != "episode":
            continue
        year = record["created"][:4] if record["created"] else "unknown"
        episode_groups[year].append(record)
    for year, group in episode_groups.items():
        manifests[f"manifest-episodes-{year}.md"] = _build_typed_manifest(group, {"episode"})

    if write:
        out_dir = vault / "manifests"
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in manifests.items():
            (out_dir / filename).write_text(content, encoding="utf-8")
    return manifests


def _priority_score(record: dict[str, Any]) -> tuple[int, str, str]:
    significance_rank = {"high": 3, "medium": 2, "low": 1}
    recency = max(record.get("updated", ""), record.get("created", ""))
    score = significance_rank.get(record.get("significance", ""), 0)
    if record.get("status") == "active":
        score += 3
    if record.get("type") == "state":
        score += 2
    if record.get("type") == "open_loop":
        score += 3
    if record.get("type") == "decision":
        score += 2
    return score, recency, record.get("id", "")


def _build_core_manifest(records: list[dict[str, Any]]) -> str:
    selected = [r for r in records if r["status"] in {"active", "stale", "resolved", "disputed"}]
    selected.sort(key=_priority_score, reverse=True)
    selected = selected[:200]
    lines = [
        "# Manifest Core",
        "",
        f"Entries: {len(selected)}",
        "",
    ]
    for record in selected:
        lines.append(f"- `{record['id']}` | {record['type']} | {record['status']} | {record['summary']} | `{record['path']}`")
    return "\n".join(lines).rstrip() + "\n"


def _build_typed_manifest(records: list[dict[str, Any]], types: set[str]) -> str:
    selected = [r for r in records if r["type"] in types]
    selected.sort(key=_priority_score, reverse=True)
    title = ", ".join(sorted(types))
    lines = [f"# Manifest {title.title()}", ""]
    for record in selected:
        lines.append(f"- `{record['id']}` | {record['status']} | {record['summary']} | `{record['path']}`")
    if len(lines) == 2:
        lines.append("- No entries")
    return "\n".join(lines).rstrip() + "\n"


def _build_archive_manifest(records: list[dict[str, Any]]) -> str:
    selected = [r for r in records if r["status"] == "archived"]
    selected.sort(key=_priority_score, reverse=True)
    lines = ["# Manifest Archive", ""]
    for record in selected:
        lines.append(f"- `{record['id']}` | {record['type']} | {record['summary']} | `{record['path']}`")
    if len(lines) == 2:
        lines.append("- No archived entries")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    generate_manifests(write=True)
    print("Manifests generated.")
    return 0

