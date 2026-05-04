from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import InterlocutorAgent, SkepticAgent
from ..frontmatter import load_markdown
from ..paths import vault_root


def review_draft(
    draft_path: Path,
    vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    vault = vault or vault_root()
    if not draft_path.exists():
        raise FileNotFoundError(draft_path)
    doc = load_markdown(draft_path)
    payload = {
        "path": str(draft_path),
        "frontmatter": doc.frontmatter,
        "skeptic": None,
        "interlocutor": None,
        "recommendation": "revise",
    }
    text = doc.body
    skeptic = SkepticAgent(vault=vault).run_json(json.dumps({"frontmatter": doc.frontmatter, "body": text}, indent=2), provider=provider, model=model)
    interlocutor = InterlocutorAgent(vault=vault).run_json(json.dumps({"frontmatter": doc.frontmatter, "body": text, "skeptic": skeptic}, indent=2), provider=provider, model=model)
    payload["skeptic"] = skeptic
    payload["interlocutor"] = interlocutor
    if skeptic.get("approved") and not skeptic.get("issues"):
        payload["recommendation"] = "promote"
        if apply:
            from .drafts import promote_draft_to_episode

            payload["promoted_path"] = str(promote_draft_to_episode(draft_path, vault))
    return payload
