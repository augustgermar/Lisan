from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class AdviceAgent(PromptAgent):
    name = "advice"
    prompt_file = "advice_v1"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        policy = _policy_dict(kwargs.get("conversation_policy"))
        _ = kwargs.get("conversation_history")
        return _fallback_answer(user_input, policy=policy)


def _policy_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fallback_answer(text: str, policy: dict[str, Any] | None = None) -> str:
    lowered = text.lower()
    tone = str((policy or {}).get("tone") or "").lower()
    opener = {
        "wry": "Yep —",
        "warm": "Yep,",
        "dry": "Yep.",
        "steady": "Yes.",
    }.get(tone, "Yep.")
    if any(term in lowered for term in ["tuna", "pasta", "mayo", "celery", "salad"]):
        if tone == "wry":
            return (
                f"{opener} tuna pasta salad is old reliable for a reason. Mix the pasta with tuna, mayo, and chopped celery, "
                "then season it with salt, pepper, and a little acid if you have it, like lemon juice or vinegar."
            )
        if tone == "warm":
            return (
                f"{opener} tuna pasta salad is a good call. Mix the pasta with tuna, mayo, and chopped celery, "
                "then season it with salt, pepper, and a little acid if you have it, like lemon juice or vinegar."
            )
        return (
            f"{opener} tuna pasta salad works. Mix the pasta with tuna, mayo, and chopped celery, "
            "then season it with salt, pepper, and a little acid if you have it, like lemon juice or vinegar."
        )
    if tone == "wry":
        return "Yep. Start with the simplest version first. The fancy version can wait its turn."
    if tone == "warm":
        return "Yep, start with the simplest version first and see what it actually needs."
    if tone == "steady":
        return "Yes. Start simple, test it, then tighten the screws."
    return "Yep. Start with the simplest version first. The fancy version can wait its turn."
