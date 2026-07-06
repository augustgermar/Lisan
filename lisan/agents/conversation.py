from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..paths import skills_root
from ..tools.execution_tools import TOOLS, build_tool_handlers
from ..tools.skill_loader import load_skills
from .base import PromptAgent


class ConversationAgent(PromptAgent):
    """The single agent that talks to the user: full rolling history, memory
    context, capabilities, and every tool. It answers in one call; memory
    capture observes the finished exchange afterwards, in the background."""

    name = "interlocutor"  # shares the interlocutor's routing/model config
    prompt_file = "conversation_v1"
    output_schema_name = "conversation_output"

    def prompt(self) -> str:
        # Identity is carried by the vault, not the prompt file: a ratified
        # kernel voice supersedes the authored ## Voice section, so an engine
        # swap carries the voice by construction. No kernel voice → the
        # authored voice stands, unchanged.
        from ..prompts import load_prompt
        from ..tools.deixis import render_deixis
        from ..tools.kernel import kernel_voice_block, splice_voice

        prompt = load_prompt(self.prompt_file)
        voice = kernel_voice_block(self.vault)
        if voice:
            prompt = splice_voice(prompt, voice)
        return render_deixis(prompt, self.prompt_audience, self.vault)

    def run_json(
        self,
        user_input: str,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        provider_error_mode: str = "fallback",
        approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
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
            approval_fn=approval_fn,
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
        if isinstance(result.data, dict) and str(result.data.get("response") or "").strip():
            return result.data
        parsed = self.parse_output(result.text)
        if isinstance(parsed, dict) and str(parsed.get("response") or "").strip():
            return parsed
        # A plain-prose reply is a valid conversation even when the JSON
        # envelope is missing — better the words than a fallback shrug.
        text = str(result.text or "").strip()
        if text and not text.startswith("{"):
            return {"response": text}
        return {"response": ""}

    accepts_prose_finale = True

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        try:
            payload = json.loads(user_input)
            message = str(payload.get("user_message") or "").strip()
        except Exception:
            message = ""
        # Name what failed: "provider failure" is opaque; "my language model
        # timed out" tells the user it's transient and worth retrying.
        note = "My language model didn't respond just now — that's a transient hiccup, not your message. Say that again and I'll take another run at it."
        return json.dumps({"response": note})
