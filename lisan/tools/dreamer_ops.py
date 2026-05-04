from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..agents import DreamerAgent
from ..frontmatter import load_markdown
from ..paths import vault_root
from ..utils import today_iso
from .primer_audit import build_primer_audit_bundle


def run_dreamer_task(
    vault: Path | None = None,
    task: str = "compress",
    provider: str | None = None,
    model: str | None = None,
) -> Path:
    vault = vault or vault_root()
    prompt_file = _prompt_for_task(task)
    bundle = _bundle_for_task(vault, task)
    agent = DreamerAgent(vault=vault, prompt_file=prompt_file)
    response = agent.run_json(bundle, significance="high", provider=provider, model=model, task=task)
    out = _output_path(vault, task)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_report(task, bundle, response), encoding="utf-8")
    return out


def _prompt_for_task(task: str) -> str:
    mapping = {
        "compress": "dreamer_compress_v1",
        "primer": "dreamer_primer_v1",
        "contradict": "dreamer_contradict_v1",
        "epoch": "dreamer_epoch_v1",
        "confidence": "dreamer_confidence_v1",
        "overfitting": "dreamer_overfitting_v1",
        "identity_anchor": "dreamer_identity_anchor_v1",
    }
    return mapping.get(task, "dreamer_compress_v1")


def _bundle_for_task(vault: Path, task: str) -> str:
    if task == "primer":
        return build_primer_audit_bundle(vault)
    if task == "contradict":
        return _bundle_recent_episodes(vault, days=120, include_states=True, include_entities=False)
    if task == "confidence":
        return _bundle_confidence(vault)
    if task == "epoch":
        return _bundle_entities(vault)
    if task == "overfitting":
        return _bundle_overfitting(vault)
    if task == "identity_anchor":
        return _bundle_identity(vault)
    return _bundle_recent_episodes(vault, days=365, include_states=True, include_entities=True)


def _bundle_recent_episodes(vault: Path, days: int, include_states: bool, include_entities: bool) -> str:
    lines: list[str] = []
    cutoff = date.today() - timedelta(days=days)
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

    if include_states:
        lines.append("## State Files")
        for path in sorted((vault / "state").glob("*.md")):
            try:
                load_markdown(path)
            except Exception:
                continue
            lines.append(f"### {path.name}")
            lines.append(path.read_text(encoding="utf-8").strip())
            lines.append("")

    if include_entities:
        lines.append("## Entities")
        for path in sorted((vault / "entities").rglob("*.md")):
            try:
                load_markdown(path)
            except Exception:
                continue
            lines.append(f"### {path.relative_to(vault)}")
            lines.append(path.read_text(encoding="utf-8").strip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_confidence(vault: Path) -> str:
    lines = ["## Confidence Candidates", ""]
    cutoff = date.today() - timedelta(days=365)
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("significance")) != "high":
            continue
        created = doc.frontmatter.get("created")
        try:
            if not created or date.fromisoformat(str(created)) >= cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_entities(vault: Path) -> str:
    lines = ["## Entity Candidates", ""]
    for path in sorted((vault / "entities").rglob("*.md")):
        try:
            load_markdown(path)
        except Exception:
            continue
        lines.append(f"### {path.relative_to(vault)}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_overfitting(vault: Path) -> str:
    lines = ["## Overfitting Candidates", ""]
    cutoff = date.today() - timedelta(days=365)
    for path in sorted((vault / "episodes").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        if str(doc.frontmatter.get("significance")) != "high":
            continue
        created = doc.frontmatter.get("created")
        try:
            if not created or date.fromisoformat(str(created)) >= cutoff:
                continue
        except ValueError:
            continue
        lines.append(f"### {path.name}")
        lines.append(path.read_text(encoding="utf-8").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_identity(vault: Path) -> str:
    lines = ["## Identity Anchors", ""]
    lines.append(_bundle_recent_episodes(vault, days=180, include_states=True, include_entities=True))
    return "\n".join(lines).rstrip() + "\n"


def _output_path(vault: Path, task: str) -> Path:
    today = today_iso()
    if task == "contradict":
        return vault / "contradictions" / f"dreamer-contradictions-{today}.md"
    return vault / "reports" / f"dreamer-{task}-{today}.md"


def _render_report(task: str, bundle: str, response: dict[str, Any]) -> str:
    return f"""# Dreamer {task.replace('_', ' ').title()}

## Response

```json
{json.dumps(response, indent=2, ensure_ascii=True)}
```

## Bundle

{bundle.strip()}
"""
