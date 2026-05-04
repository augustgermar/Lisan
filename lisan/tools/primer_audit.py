from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..frontmatter import load_markdown
from ..agents import DreamerAgent
from ..paths import vault_root
from ..utils import today_iso


def build_primer_audit_bundle(vault: Path | None = None) -> str:
    vault = vault or vault_root()
    lines: list[str] = []

    for rel in ["primer/operating-style.md"]:
        path = vault / rel
        if path.exists():
            lines.append(f"## {rel}")
            lines.append(path.read_text(encoding="utf-8").strip())
            lines.append("")

    lines.append("## State Files")
    for path in sorted((vault / "state").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")

    lines.append("## Entities")
    for path in sorted((vault / "entities").rglob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        lines.append(f"### {path.relative_to(vault)}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")

    cutoff = date.today() - timedelta(days=90)
    lines.append("## Recent Episodes")
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        created = doc.frontmatter.get("created")
        if not created:
            continue
        try:
            if date.fromisoformat(str(created)) < cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def run_primer_audit(
    vault: Path | None = None,
    dry_run: bool = False,
    provider: str = "anthropic",
    model: str | None = None,
) -> str:
    vault = vault or vault_root()
    bundle = build_primer_audit_bundle(vault)
    if dry_run:
        return bundle

    response = DreamerAgent(vault=vault, prompt_file="dreamer_primer_v1").run_json(
        bundle,
        significance="high",
        provider=provider,
        model=model,
        task="primer",
    )
    out = vault / "reports" / f"primer-audit-draft-{today_iso()}.md"
    if isinstance(response, dict):
        out.write_text(
            "# Primer Audit Draft\n\n```json\n" + json.dumps(response, indent=2, ensure_ascii=True) + "\n```\n",
            encoding="utf-8",
        )
    else:
        out.write_text(str(response), encoding="utf-8")
    return str(out)
