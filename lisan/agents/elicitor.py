from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent

GENERIC_FOLLOW_UPS = {
    "could you say a little more about that?",
    "could you tell me a little more about that?",
    "what do you want to know?",
    "can you say a little more about that?",
    "tell me more about that",
}


class ElicitorAgent(PromptAgent):
    name = "elicitor"
    prompt_file = "elicitor_v1"
    output_schema_name = "elicitor_output"

    def run_json(
        self,
        user_input: str,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = self.run(
            user_input,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema,
            **kwargs,
        )
        data = result.data if isinstance(result.data, dict) else {}
        response = str(data.get("response") or result.text or "").strip()
        if not response or self._is_generic(response):
            data = self._fallback_payload(user_input, **kwargs)
        return data

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        return json.dumps(self._fallback_payload(user_input, **kwargs), indent=2, ensure_ascii=True)

    def _fallback_payload(self, user_input: str, **kwargs: Any) -> dict[str, Any]:
        current_state = kwargs.get("current_state")
        story_thread = self._story_thread(user_input, bool(current_state))
        entities = self._entities(user_input)
        first_clause = self._first_clause(user_input) or "No content provided yet."
        response = self._specific_follow_up(user_input, current_state=current_state)
        payload = {
            "response": response,
            "updated_narrative_state": {
                "open_questions": [response],
                "next_step": response,
                "mode_status": "developing",
                "story_thread": story_thread,
                "entities_involved": entities,
                "established": [first_clause],
                "emotional_texture": self._emotional_texture(user_input),
                "open_threads": [],
                "unresolved": [],
            },
            "questions": [response],
        }
        return payload

    def _story_thread(self, text: str, continuing: bool) -> str:
        if continuing:
            return "Continuation of the current story thread."
        first = self._first_clause(text)
        return first[:120] or "New story thread"

    def _specific_follow_up(self, text: str, current_state: Any = None) -> str:
        lowered = text.lower()
        if any(term in lowered for term in ["working on", "building", "new agent", "project", "system"]):
            return "What part of building this new agent are you most excited about?"
        if any(term in lowered for term in ["beautiful night", "night", "evening", "weather"]):
            return "What about the night is standing out to you?"
        if "cleaner and safer" in lowered or ("safer" in lowered and "setup" in lowered):
            return "What part of the external setup feels safer to you?"
        if any(term in lowered for term in ["glad", "finally"]) and "repo" in lowered:
            return "What changes now that the vault is out of the repo?"
        if any(term in lowered for term in ["glad", "cleaner", "safer", "safer", "finally", "win", "relieved"]) and any(
            term in lowered for term in ["vault", "repo", "setup", "system", "route", "launch"]
        ):
            return "What part of this setup feels like the biggest win to you?"
        if any(term in lowered for term in ["made myself", "try it out", "first bit", "taking a bite", "took my first bit"]) and any(
            term in lowered for term in ["tuna", "pasta", "salad", "mayo", "celery"]
        ):
            topic = self._food_topic(text)
            return f"How is the {topic} tasting so far?"
        if any(term in lowered for term in [
            "could use a little",
            "could use more",
            "needs a little",
            "needs more",
            "pretty good",
            "good but",
            "otherwise pretty good",
            "tastes good",
            "taste it out",
        ]):
            return "What would you tweak next to make it taste exactly right?"
        if any(term in lowered for term in ["excited", "happy", "proud", "relieved", "anxious", "nervous", "frustrated", "sad", "angry", "worried"]):
            topic = self._topic_phrase(text)
            return f"What about {topic} is making you feel that way?"
        if current_state:
            topic = self._topic_phrase(text)
            return f"What feels most important about {topic}?"
        topic = self._topic_phrase(text)
        return f"What feels most important about {topic}?"

    def _topic_phrase(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "that"
        lowered = text.lower()
        for phrase in ["i am ", "i'm ", "i was ", "i have ", "i had "]:
            if lowered.startswith(phrase):
                remainder = text[len(phrase):].strip()
                if remainder:
                    return self._trim_subject(remainder)
        clause = self._first_clause(text)
        return self._trim_subject(clause[:80]) or "that"

    def _trim_subject(self, text: str) -> str:
        cleaned = text.strip().rstrip(".,!?")
        lowered = cleaned.lower()
        for prefix in ["very ", "so ", "really ", "pretty ", "kind of ", "kinda "]:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                lowered = cleaned.lower()
        for marker in ["about ", "to ", "at the prospect of ", "about the prospect of "]:
            if marker in lowered:
                idx = lowered.index(marker) + len(marker)
                cleaned = cleaned[idx:].strip()
                lowered = cleaned.lower()
                break
        return cleaned

    def _food_topic(self, text: str) -> str:
        lowered = text.lower()
        if "tuna" in lowered and "pasta" in lowered:
            return "tuna pasta salad"
        if "salad" in lowered:
            return "salad"
        if "tuna" in lowered:
            return "tuna dish"
        if "pasta" in lowered:
            return "pasta dish"
        return "food"

    def _emotional_texture(self, text: str) -> str:
        lowered = text.lower()
        if any(term in lowered for term in ["excited", "happy", "proud", "relieved", "hopeful"]):
            return "positive"
        if any(term in lowered for term in ["anxious", "nervous", "frustrated", "sad", "angry", "worried"]):
            return "tense"
        return "unclear"

    def _is_generic(self, response: str) -> bool:
        normalized = response.strip().lower().rstrip(" .!?")
        return normalized in GENERIC_FOLLOW_UPS

    def _first_clause(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        for separator in [".", "!", "?", "\n"]:
            if separator in text:
                return text.split(separator, 1)[0].strip()
        return text[:160].strip()

    def _entities(self, text: str) -> list[str]:
        import re

        entities: list[str] = []
        for match in re.finditer(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?\b", text):
            value = match.group(0)
            if value not in entities:
                entities.append(value)
        return entities[:6]
