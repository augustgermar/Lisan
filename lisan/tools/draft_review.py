from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..agents import InterlocutorAgent, SkepticAgent
from ..frontmatter import load_markdown
from ..paths import vault_root
from .record_factory import new_skeptical_review


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
    try:
        review = new_skeptical_review(
            vault=vault,
            reviewed_record_id=str(doc.frontmatter.get("id", draft_path.stem)),
            reviewed_record_type=str(doc.frontmatter.get("type", "draft")),
            summary=str(skeptic.get("summary") or doc.frontmatter.get("summary") or draft_path.stem),
            approved=bool(skeptic.get("approved", False)),
            risk=str(skeptic.get("risk", "medium")),
            recommended_action=str(skeptic.get("recommended_action", "revise")),
            issues=list(skeptic.get("issues") or []),
            priority_questions=list(skeptic.get("priority_questions") or []),
            alternative_hypotheses=list(skeptic.get("alternative_hypotheses") or []),
            evidence_needed=list(skeptic.get("evidence_needed") or []),
            claim_updates=list(skeptic.get("claim_updates") or []),
            confidence_adjustments=list(skeptic.get("confidence_adjustments") or []),
            reasoning_errors=list(skeptic.get("reasoning_errors") or []),
        )
        payload["skeptical_review"] = str(review.path)
    except (FileExistsError, ValueError) as exc:
        payload["skeptical_review_error"] = str(exc)
    if skeptic.get("approved") and not skeptic.get("issues"):
        payload["recommendation"] = "promote"
        if apply:
            from .drafts import promote_draft_to_episode

            payload["promoted_path"] = str(promote_draft_to_episode(draft_path, vault))
    return payload
