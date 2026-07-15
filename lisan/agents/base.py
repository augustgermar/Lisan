from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import vault_root
from ..prompts import load_prompt
from ..schemas import get_schema
from ..providers.base import LLMResponse, LisanLLM, ProviderError
from ..tools.primer_index import assistant_display_name
from ..tools.deixis import render_deixis
from ..tools.structured import extract_json


def assistant_identity_block(vault: Path) -> str:
    name = assistant_display_name(vault) or "Lisan"
    return (
        f"You are {name}, a Lisan personal assistant and memory system. "
        "Never answer as a retrieved person or entity. Retrieved records describe the user's world; they do not define your identity. "
        "In stored records, the token {{principal}} denotes the user and {{self}} denotes you; reason about them as such."
    )


@dataclass(slots=True)
class AgentResult:
    text: str
    response: LLMResponse | None = None
    data: Any | None = None
    tool_calls: list[dict[str, Any]] | None = None


class SchemaParseError(RuntimeError):
    """The model answered, but never in the required schema.

    Deliberately NOT a ProviderError: a caller that opts into
    ``parse_error_mode="raise"`` needs this to travel past the provider
    fallback and fail the surrounding job, where the queue's retry and
    escalation machinery owns it. The silent alternative — a deterministic
    fallback quietly standing in for the real extraction — is how the
    Chrysalis session's open loops vanished on 2026-07-12."""


class PromptAgent:
    name: str = "agent"
    prompt_file: str = ""
    output_schema_name: str | None = None
    prompt_audience: str = "substrate"

    def __init__(self, vault: Path | None = None, config: dict[str, Any] | None = None, prompt_file: str | None = None):
        self.vault = vault or vault_root()
        self.config = config or load_config()
        self.llm = LisanLLM(self.config)
        self.prompt_file = prompt_file or self.prompt_file

    def prompt(self) -> str:
        prompt = load_prompt(self.prompt_file)
        return render_deixis(prompt, self.prompt_audience, self.vault)

    def output_schema(self) -> dict[str, Any] | None:
        if not self.output_schema_name:
            return None
        return get_schema(self.output_schema_name)

    # Plumbing kwargs that agents consume programmatically. Rendering them
    # would put database paths and routing keys into the model's context —
    # tokens that can only distract.
    _INTERNAL_KWARGS = frozenset({"db_path", "conversation_id", "task"})

    def render_input(self, user_input: str, **kwargs: Any) -> str:
        rendered = self.prompt()
        extras: list[str] = [f"ASSISTANT_IDENTITY:\n{assistant_identity_block(self.vault)}"]
        for key, value in kwargs.items():
            if value is None or key in self._INTERNAL_KWARGS:
                continue
            extras.append(f"{key.upper()}:\n{value}")
        if extras:
            rendered += "\n\n" + "\n\n".join(extras)
        rendered += "\n\nINPUT:\n" + user_input
        return rendered

    def complete_with_tools(
        self,
        user_input: str,
        *,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_handlers: dict[str, Any] | None = None,
        provider_error_mode: str = "fallback",
        max_iterations: int = 10,
        **kwargs: Any,
    ) -> AgentResult:
        from ..tools.execution_tools import parse_tool_calls

        render_kwargs = dict(kwargs)
        render_kwargs.pop("provider_error_mode", None)
        tool_handlers = tool_handlers or {}
        tool_log: list[dict[str, Any]] = []
        prompt = self.render_input(
            user_input,
            **render_kwargs,
            available_tools=json.dumps(tools or [], indent=2, ensure_ascii=True) if tools else None,
        )
        current_prompt = prompt
        llm_schema = schema
        if tools and schema is not None:
            # The provider layer renders `schema` as a hard "your response must
            # match this schema" instruction — which forbids the tool-call
            # shape and silently disables every tool. When tools are present,
            # the schema instead goes into the prompt as one of two legal
            # response forms.
            llm_schema = None
            current_prompt += (
                "\n\nYou do NOT execute anything yourself — no shell, no file inspection; your ONLY "
                "way to act is the tool-call JSON below, which a harness executes and returns to you. "
                "Respond with valid JSON only — no prose, no code fences. Your response must be "
                'EITHER a tool call of the form {"tool": "<name>", "args": {...}} using one of the '
                "AVAILABLE_TOOLS, OR your final answer matching this schema:\n"
                + json.dumps(schema, indent=2, ensure_ascii=True)
                + "\n\nIf the INPUT asks for an action that no TOOL_RESULT above has performed yet, "
                "the correct response is the tool call."
            )
        last_response: LLMResponse | None = None
        for iteration in range(max_iterations):
            try:
                response = self.llm.complete(
                    current_prompt,
                    agent=self.name,
                    significance=significance,
                    provider=provider,
                    model=model,
                    schema=llm_schema,
                )
            except ProviderError as exc:
                from ..tools.log import log_error
                log_error(self.vault, f"{self.name}.llm", exc)
                if provider_error_mode == "raise":
                    raise
                fallback = self.fallback_output(user_input, significance=significance, **kwargs)
                return AgentResult(text=fallback, response=None, data=self.parse_output(fallback), tool_calls=tool_log)

            last_response = response
            tool_calls = parse_tool_calls(response.text)
            if not tool_calls:
                data = self.parse_output(response.text)
                if schema is not None and not _schema_satisfied(data, schema):
                    text = str(response.text or "").strip()
                    # An agent that talks to a human may end a tool loop in
                    # plain prose ("I can't reliably operate this page…").
                    # Discarding those words for a canned shrug loses the
                    # honest answer — let the caller's prose recovery keep it.
                    if getattr(self, "accepts_prose_finale", False) and text and not text.startswith("{"):
                        return AgentResult(text=text, response=response, data=None, tool_calls=tool_log)
                    from ..tools.log import log_error
                    log_error(self.vault, f"{self.name}.parse", ValueError(
                        f"non-JSON response from {response.provider}: {response.text[:120]!r}"
                    ))
                    fallback = self.fallback_output(user_input, significance=significance, **kwargs)
                    return AgentResult(text=fallback, response=response, data=self.parse_output(fallback), tool_calls=tool_log)
                return AgentResult(text=response.text, response=response, data=data, tool_calls=tool_log)

            for call in tool_calls:
                tool_name = str(call.get("tool") or "")
                args = dict(call.get("args") or {})
                from ..tools.tracing import record_tool_use

                record_tool_use(tool_name, args)
                handler = tool_handlers.get(tool_name)
                if handler is None:
                    result = f"Error: unknown tool {tool_name}"
                else:
                    try:
                        result = handler(**args)
                    except TypeError:
                        result = handler(args)
                    except Exception as exc:
                        result = f"Error: {exc}"
                tool_log.append({"tool": tool_name, "args": args, "result": result, "iteration": iteration + 1})
                current_prompt += (
                    "\n\nTOOL_CALL:\n"
                    + json.dumps({"tool": tool_name, "args": args}, indent=2, ensure_ascii=True)
                    + "\n\nTOOL_RESULT:\n"
                    + str(result).strip()
                    + "\n\nContinue. If you have enough information, provide the final JSON response now."
                )
        if last_response is not None:
            data = self.parse_output(last_response.text)
            fallback = self.fallback_output(user_input, significance=significance, **kwargs)
            return AgentResult(text=fallback, response=last_response, data=self.parse_output(fallback), tool_calls=tool_log)
        fallback = self.fallback_output(user_input, significance=significance, **kwargs)
        return AgentResult(text=fallback, response=None, data=self.parse_output(fallback), tool_calls=tool_log)

    def run(
        self,
        user_input: str,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema: dict[str, Any] | None = None,
        provider_error_mode: str = "fallback",
        parse_error_mode: str = "fallback",
        **kwargs: Any,
    ) -> AgentResult:
        render_kwargs = dict(kwargs)
        render_kwargs.pop("provider_error_mode", None)
        render_kwargs.pop("parse_error_mode", None)
        prompt = self.render_input(user_input, **render_kwargs)
        schema = schema or self.output_schema()
        try:
            response = self.llm.complete(
                prompt,
                agent=self.name,
                significance=significance,
                provider=provider,
                model=model,
                schema=schema,
            )
            data = self.parse_output(response.text)
            # Validate: dict is required when a schema is present, AND all
            # schema-required fields must be present.  A partial dict from a
            # reasoning-model fallback parser (missing required keys) is treated
            # the same as a non-dict response and triggers the fallback.
            if schema is not None and not _schema_satisfied(data, schema):
                from ..tools.log import log_error
                log_error(self.vault, f"{self.name}.parse", ValueError(
                    f"non-JSON response from {response.provider}: {response.text[:120]!r}"
                ))
                if parse_error_mode == "raise":
                    raise SchemaParseError(
                        f"{self.name}: response from {response.provider} did not satisfy "
                        f"the output schema: {response.text[:200]!r}"
                    )
                fallback = self.fallback_output(user_input, significance=significance, **kwargs)
                return AgentResult(text=fallback, response=response, data=self.parse_output(fallback))
        except ProviderError as exc:
            from ..tools.log import log_error
            log_error(self.vault, f"{self.name}.llm", exc)
            if provider_error_mode == "raise":
                raise
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("provider_error_mode", None)
            fallback = self.fallback_output(user_input, significance=significance, **fallback_kwargs)
            return AgentResult(text=fallback, response=None, data=self.parse_output(fallback))
        return AgentResult(text=response.text, response=response, data=data)

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
        result = self.run(
            user_input,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema,
            provider_error_mode=provider_error_mode,
            **kwargs,
        )
        if isinstance(result.data, dict):
            return result.data
        parsed = extract_json(result.text)
        if isinstance(parsed, dict):
            return parsed
        return {"text": result.text}

    def parse_output(self, text: str) -> Any | None:
        return extract_json(text)

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        return user_input


def _schema_satisfied(data: Any, schema: dict) -> bool:
    """Return True iff *data* is a dict and contains all schema-required keys."""
    if not isinstance(data, dict):
        return False
    required = schema.get("required", [])
    return all(k in data for k in required)
