from __future__ import annotations

from typing import Any

from .base import PromptAgent


class AdviceAgent(PromptAgent):
    name = "advice"
    prompt_file = "advice_v1"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        _ = kwargs.get("conversation_history")
        return _fallback_answer(user_input)


def _fallback_answer(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ["tuna", "pasta", "mayo", "celery", "salad"]):
        return (
            "Yep, that works. Tuna pasta salad is the old reliable for a reason. Mix the pasta with tuna, mayo, and chopped celery, "
            "then season it with salt, pepper, and a little acid if you have it, like lemon juice or vinegar."
        )
    return "Yep. Start with the simplest version first. The fancy version can wait its turn."
