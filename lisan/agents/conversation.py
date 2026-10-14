from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..tools.execution_tools import agent_tools, build_tool_handlers
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
        tools = agent_tools()
        tool_handlers = build_tool_handlers(
            vault=self.vault,
            db_path=kwargs.get("db_path"),
            config=self.config,
            conversation_id=kwargs.get("conversation_id"),
            domain=kwargs.get("domain"),
            approval_fn=approval_fn,
        )
        # Ordinary turns answer in plain prose: memory capture reads the
        # finished transcript afterward (run_conversation_turn appends
        # `response` verbatim and hands it to capture.observe) and never
        # touches this envelope, so forcing every reply through a JSON
        # wrapper buys the pipeline nothing but stiffer language. The one
        # turn type that still needs structure alongside the words is an
        # interpretation-protocol turn, where the deterministic IIP
        # validator checks the "interpretation" object — so only request
        # the schema then.
        needs_schema = kwargs.get("interpretation_protocol") is not None
        result = self.complete_with_tools(
            user_input,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema if schema is not None else (self.output_schema() if needs_schema else None),
            tools=tools,
            tool_handlers=tool_handlers,
            provider_error_mode=provider_error_mode,
            **kwargs,
        )
        unavailable_tools = self._registered_unavailable_tools(result, tools)
        if unavailable_tools:
            recovery_input = (
                user_input
                + "\n\nINTERNAL TOOL-RECOVERY INSTRUCTION: Your previous response claimed that "
                + ", ".join(unavailable_tools)
                + " was unavailable, but the live registry for this turn contains that tool. "
                + "Do not repeat the claim. If the user's request requires an action, call the "
                + "appropriate registered tool now using the exact JSON tool-call format. "
                + "If no action is required, state only that you did not call the tool."
            )
            recovery = self.complete_with_tools(
                recovery_input,
                significance=significance,
                provider=provider,
                model=model,
                schema=schema if schema is not None else (self.output_schema() if needs_schema else None),
                tools=tools,
                tool_handlers=tool_handlers,
                provider_error_mode=provider_error_mode,
                max_iterations=min(int(kwargs.get("max_iterations", 10)), 3),
                **{k: v for k, v in kwargs.items() if k != "max_iterations"},
            )
            if recovery.tool_calls:
                result = recovery
            else:
                result.data = {
                    **(result.data if isinstance(result.data, dict) else {}),
                    "response": (
                        "I did not call " + ", ".join(unavailable_tools)
                        + "; it is registered, but no action was performed."
                    ),
                }
                result.text = json.dumps(result.data, ensure_ascii=True)
        # Tool calls are prompt-mediated, so a provider can return ordinary
        # prose even though the tool is present. If it then claims check-in is
        # unavailable, give the same turn one narrow recovery attempt. This
        # path is deliberately dormant unless that exact false claim appears;
        # ordinary turns pay no extra inference cost.
        if (
            not any(call.get("tool") == "checkin" for call in (result.tool_calls or []))
            and self._claims_checkin_unavailable(result)
        ):
            checkin_tool = next((tool for tool in tools if tool.get("name") == "checkin"), None)
            checkin_handler = tool_handlers.get("checkin")
            if checkin_tool is not None and checkin_handler is not None:
                recovery_input = (
                    user_input
                    + "\n\nINTERNAL TOOL-RECOVERY INSTRUCTION: Your previous draft incorrectly said the check-in tool was unavailable. "
                    "Review the actual user message in the input. If it reports an observed state or event about a tracked person, "
                    "call the available checkin tool now, using only the observation and the correct person. Do not infer a diagnosis "
                    "or invent context. If the user was only discussing tool availability and gave no observation, do not call it. "
                    "If you do not call it, say plainly that no check-in was logged; never say the tool is unavailable."
                )
                recovery = self.complete_with_tools(
                    recovery_input,
                    significance=significance,
                    provider=provider,
                    model=model,
                    schema=(schema or self.output_schema()) if needs_schema else None,
                    tools=[checkin_tool],
                    tool_handlers={"checkin": checkin_handler},
                    provider_error_mode=provider_error_mode,
                    max_iterations=min(int(kwargs.get("max_iterations", 10)), 3),
                    **{k: v for k, v in kwargs.items() if k != "max_iterations"},
                )
                recovery_calls = recovery.tool_calls or []
                if any(call.get("tool") == "checkin" for call in recovery_calls):
                    result = recovery
                    result.tool_calls = (result.tool_calls or [])
                else:
                    # The recovery model may still fail to emit the tool-call
                    # shape. Do not pass its unsupported availability claim on.
                    old = recovery.data if isinstance(recovery.data, dict) else {}
                    result = recovery
                    result.data = {**old, "response": "I didn’t log that check-in. I don’t know why I missed the tool call."}
                    result.text = json.dumps(result.data, ensure_ascii=True)
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

    @staticmethod
    def _claims_checkin_unavailable(result: Any) -> bool:
        """Catch unsupported claims that the check-in capability is absent."""
        data = result.data if isinstance(result.data, dict) else {}
        text = str(data.get("response") or result.text or "").lower().replace("’", "'")
        text = re.sub(r"\s+", " ", text)
        return "check-in" in text and any(phrase in text for phrase in (
            "isn't available", "is not available", "not available", "unavailable",
            "isn't accessible", "is not accessible", "can't access",
        ))

    @staticmethod
    def _registered_unavailable_tools(result: Any, tools: list[dict[str, Any]]) -> list[str]:
        """Return registered tools the model falsely described as absent."""
        data = result.data if isinstance(result.data, dict) else {}
        text = str(data.get("response") or result.text or "").lower().replace("’", "'")
        normalized = re.sub(r"\s+", " ", text)
        unavailable = (
            "not available", "isn't available", "is not available", "unavailable",
            "not exposed", "isn't exposed", "is not exposed", "not accessible",
        )
        generic_unavailable = (
            "doesn't expose", "does not expose", "didn't expose", "did not expose",
            "don't expose", "do not expose",
        )
        neither_exposed = bool(re.search(
            r"\bneither\b.+\bnor\b.+\b(?:is|are|was|were)\s+(?:not\s+)?exposed\b",
            normalized,
        ))
        if not (
            any(phrase in normalized for phrase in unavailable)
            or any(phrase in normalized for phrase in generic_unavailable)
            or neither_exposed
        ):
            return []
        called = {str(call.get("tool") or "") for call in (result.tool_calls or [])}
        # Models sometimes describe a capability by role rather than by its
        # registered name ("the printer or executor tools"). Map only these
        # unambiguous domain terms; do not guess from a generic "tools" claim.
        aliases = {
            "printer-management": ("printer", "printing", "cups", "jetdirect"),
            "execute_task": ("executor", "execution", "shell command", "cli command"),
        }
        registered: list[str] = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if (
                name
                and name not in called
                and re.search(
                    rf"(?<![a-z0-9_-]){re.escape(name.lower())}(?![a-z0-9_-])",
                    normalized,
                )
            ):
                registered.append(name)
                continue
            if (
                name
                and name not in called
                and any(alias in normalized for alias in aliases.get(name, ()))
            ):
                registered.append(name)
        return registered

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
