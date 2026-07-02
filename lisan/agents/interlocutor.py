from __future__ import annotations

import json
from typing import Any

from ..paths import skills_root
from ..tools.operating_style import load_operating_style
from ..tools.execution_tools import TOOLS, build_tool_handlers
from ..tools.skill_loader import load_skills
from .base import PromptAgent


class InterlocutorAgent(PromptAgent):
    name = "interlocutor"
    prompt_file = "interlocutor_v1"
    output_schema_name = "interlocutor_output"
    def parse_output(self, text: str) -> Any | None:
        """Finding #11: reject LLM output that parses but lacks the required
        ``response`` field. The default ``extract_json`` can return a partial
        dict (e.g. ``{"text": "..."}``) when the bullet-fallback parser fires;
        accepting that lets prose narration leak through as the user-facing
        response. Forcing a None return routes through ``fallback_output``
        instead, which composes a safe acknowledgement from the writer's
        structured output.
        """
        parsed = super().parse_output(text)
        if not isinstance(parsed, dict):
            return None
        response = parsed.get("response")
        if not isinstance(response, str) or not response.strip():
            return None
        return parsed

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        """Finding #10 + #11: build a user-safe acknowledgement from the JSON
        payload that ``_interlocutor_input`` passes in (writer summary,
        decisions, entities). Honors persona preferences from
        ``primer/operating-style.md`` when present.
        """
        payload_in = self._safe_parse(user_input)
        style = load_operating_style(self.vault)
        response = self._compose_response(payload_in, style)
        questions = self._questions_from_payload(payload_in, user_input)
        out = {
            "response": response,
            "questions": questions,
            "recommended_action": "review_later" if questions else "auto_commit",
            "updated_narrative_state": {
                "open_questions": questions,
                "next_step": "Follow the user's lead.",
            },
        }
        return json.dumps(out, indent=2, ensure_ascii=True)

    def run_json(
        self,
        user_input: str,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        provider_error_mode: str = "fallback",
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.last_tool_calls = []
        tools = list(TOOLS) + load_skills(skills_root())
        tool_handlers = build_tool_handlers(
            vault=self.vault,
            db_path=kwargs.get("db_path"),
            config=self.config,
            conversation_id=kwargs.get("conversation_id"),
            domain=kwargs.get("domain"),
        )
        result = self.complete_with_tools(
            user_input,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema or self.output_schema(),
            tools=tools,
            tool_handlers=tool_handlers,
            provider_error_mode=provider_error_mode,
            **kwargs,
        )
        self.last_tool_calls = result.tool_calls or []
        if isinstance(result.data, dict):
            return result.data
        parsed = self.parse_output(result.text)
        if isinstance(parsed, dict):
            return parsed
        return {"response": result.text, "questions": [], "updated_narrative_state": {}}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _safe_parse(text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _compose_response(self, payload: dict[str, Any], style: dict[str, Any]) -> str:
        summary = str(payload.get("writer_summary") or "").strip()
        decisions = [d for d in (payload.get("decisions") or []) if isinstance(d, str) and d.strip()]
        emotion_naming = style.get("emotion-naming")
        directness = style.get("directness") is True

        if decisions:
            # Decisive turn → decisive acknowledgement.
            first = decisions[0].strip().rstrip(".")
            return f"Noted — {first}."

        if summary:
            # Preserve the summary's original capitalization. Lowercasing the
            # lead character can mangle proper nouns ("Marcus pulled..." →
            # "marcus pulled..."); the marginal grammatical awkwardness of
            # capital-letter-after-em-dash is the lesser evil.
            if directness:
                return f"Heard: {summary.rstrip('.')}."
            if emotion_naming is False:
                # Avoid affect-laden openers; mirror the factual content.
                return f"Got it — {summary}"
            return f"Got it — {summary}"

        # No structured content to mirror. Final-tier acknowledgement that
        # never claims to need more detail, never invents emotional texture.
        return "Heard. Say more when you're ready."

    def _questions_from_payload(self, payload: dict[str, Any], raw_input: str) -> list[str]:
        writer_questions = [q for q in (payload.get("writer_questions") or [])
                            if isinstance(q, str) and q.strip()]
        if writer_questions:
            return writer_questions[:3]
        # Fall back to the legacy heuristic: pick lines ending with "?".
        questions: list[str] = []
        for line in raw_input.splitlines():
            line = line.strip()
            if line.endswith("?"):
                questions.append(line)
        return questions[:3]
