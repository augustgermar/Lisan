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
        tool_handlers = self._once_per_turn(tool_handlers)
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
        # The owner's ceiling applied only to the interlocutor; this agent
        # always stopped at the hard-coded 10.
        conversation_cfg = (self.config or {}).get("conversation") or {}
        kwargs.setdefault(
            "max_iterations",
            max(1, min(int(conversation_cfg.get("max_tool_iterations") or 10), 60)),
        )
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
            instruction = (
                "INTERNAL TOOL-RECOVERY INSTRUCTION: Your previous response claimed that "
                + ", ".join(unavailable_tools)
                + " was unavailable, but the live registry for this turn contains that tool. "
                + "Do not repeat the claim. If the user's request requires an action, call the "
                + "appropriate registered tool now using the exact JSON tool-call format. "
                + "If no action is required, state only that you did not call the tool."
            )
            correction = (
                "I did not call " + ", ".join(unavailable_tools)
                + "; it is registered, but no action was performed."
            )
            result = self._run_hallucination_recovery(
                result=result,
                user_input=user_input,
                instruction=instruction,
                strip_fn=lambda text: self._strip_unavailable_claim_sentences(text, unavailable_tools),
                correction=correction,
                tools=tools,
                tool_handlers=tool_handlers,
                significance=significance,
                provider=provider,
                model=model,
                schema=schema if schema is not None else (self.output_schema() if needs_schema else None),
                provider_error_mode=provider_error_mode,
                max_iterations=kwargs.get("max_iterations", 10),
                extra_kwargs={k: v for k, v in kwargs.items() if k != "max_iterations"},
            )
        unconfirmed_actions = self._unconfirmed_action_claims(result, tools)
        if unconfirmed_actions:
            instruction = (
                "INTERNAL TOOL-RECOVERY INSTRUCTION: Your previous response claimed you had "
                "already completed an action (" + ", ".join(unconfirmed_actions) + ") but no "
                "matching tool call was made this turn — that claim is false and must not be "
                "repeated. Either call the real tool now with correct arguments, or tell the "
                "user plainly that nothing was done yet."
            )
            correction = (
                "I did not actually call " + ", ".join(unconfirmed_actions)
                + " this turn, so that hasn't happened yet."
            )
            result = self._run_hallucination_recovery(
                result=result,
                user_input=user_input,
                instruction=instruction,
                strip_fn=lambda text: self._strip_action_claim_sentences(text, unconfirmed_actions),
                correction=correction,
                tools=tools,
                tool_handlers=tool_handlers,
                significance=significance,
                provider=provider,
                model=model,
                schema=schema if schema is not None else (self.output_schema() if needs_schema else None),
                provider_error_mode=provider_error_mode,
                max_iterations=kwargs.get("max_iterations", 10),
                extra_kwargs={k: v for k, v in kwargs.items() if k != "max_iterations"},
            )
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

    # Tools whose repetition has no effect beyond returning fresh data; a
    # re-run of these within a turn should see current state, not a memo.
    READ_ONLY_TOOLS = frozenset({"search_memory", "read_file", "self_state", "skill"})

    def _once_per_turn(self, handlers: dict[str, Callable[..., Any]]) -> dict[str, Callable[..., Any]]:
        """A turn can run the tool loop more than once — the empty-response
        retry, interpretation-protocol regeneration, check-in recovery — and
        each pass re-executes whatever tool calls the model repeats. Every
        side-effecting call is therefore memoized for the life of this agent
        (one agent per turn): an identical repeat returns the first result,
        marked as reused, instead of sending, scheduling, or delegating
        again."""
        from .base import invoke_tool

        memo: dict[str, Any] = self.__dict__.setdefault("_turn_tool_memo", {})
        wrapped: dict[str, Callable[..., Any]] = {}
        for name, handler in handlers.items():
            if name in self.READ_ONLY_TOOLS:
                wrapped[name] = handler
                continue

            def _call(*, _name=name, _handler=handler, **args: Any) -> Any:
                key = _name + "\x00" + json.dumps(args, sort_keys=True, ensure_ascii=True, default=str)
                if key in memo:
                    return (
                        "(Already executed earlier in this turn with identical arguments; "
                        "not run again. Original result follows.)\n" + str(memo[key])
                    )
                result = invoke_tool(_handler, args, tool_name=_name)
                memo[key] = result
                return result

            wrapped[name] = _call
        return wrapped

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

    # Models sometimes describe a capability by role rather than by its
    # registered name ("the printer or executor tools"). Map only these
    # unambiguous domain terms; do not guess from a generic "tools" claim.
    _TOOL_ALIASES = {
        "printer-management": ("printer", "printing", "cups", "jetdirect"),
        "execute_task": ("executor", "execution", "shell command", "cli command"),
    }

    _UNAVAILABLE_PHRASES = (
        "not available", "isn't available", "is not available", "unavailable",
        "not exposed", "isn't exposed", "is not exposed", "not accessible",
        "isn't accessible", "is not accessible", "no access to",
        "don't have access", "do not have access", "can't access", "cannot access",
        "not able to use", "unable to use", "doesn't support", "does not support",
    )
    _GENERIC_UNAVAILABLE_PHRASES = (
        "doesn't expose", "does not expose", "didn't expose", "did not expose",
        "don't expose", "do not expose",
    )
    # "I don't have the GitHub executor available in this channel" — a common
    # hedge that doesn't contain any literal "not available" substring.
    _UNAVAILABLE_HEDGE_PATTERNS = (
        re.compile(r"\b(?:don'?t|doesn'?t|do not|does not)\s+have\b[^.!?]{0,60}\bavailable\b"),
    )
    _NEITHER_EXPOSED_RE = re.compile(
        r"\bneither\b.+\bnor\b.+\b(?:is|are|was|were)\s+(?:not\s+)?exposed\b",
    )

    # Side-effecting tools whose claimed completion must be backed by an
    # actual tool call this turn. Phrases are first-person, past-tense
    # completion claims; kept narrow to avoid flagging ordinary conversation
    # about past or hypothetical actions.
    _ACTION_CLAIM_PHRASES: dict[str, tuple[str, ...]] = {
        "send_email": (
            "email sent", "sent the email", "sent an email", "i've sent", "i just sent",
            "email has been sent", "relay accepted", "message was delivered",
            "mail was sent", "email is on its way", "the email went out",
        ),
        "schedule_task": (
            "i scheduled", "i've scheduled", "reminder is set", "task is scheduled",
            "scheduling complete", "i set a reminder", "i've set a reminder",
        ),
        "create_plan": (
            "i created a plan", "i've created the plan", "plan has been created",
            "plan is now running", "the plan is in motion",
        ),
        "checkin": (
            "logged a check-in", "check-in logged", "i logged the check-in",
            "i've logged a check-in",
        ),
        "merge_entities": (
            "i merged", "i've merged", "merge is complete", "entities have been merged",
        ),
        "ingest_files": (
            "i ingested", "i've ingested", "ingestion is complete", "files have been ingested",
        ),
    }

    _SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

    @classmethod
    def _split_sentences(cls, text: str) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []
        return [s for s in cls._SENTENCE_SPLIT_RE.split(stripped) if s.strip()]

    @staticmethod
    def _normalize(sentence: str) -> str:
        return re.sub(r"\s+", " ", sentence.lower().replace("’", "'"))

    @classmethod
    def _sentence_claims_unavailability(cls, normalized: str) -> bool:
        return (
            any(phrase in normalized for phrase in cls._UNAVAILABLE_PHRASES)
            or any(phrase in normalized for phrase in cls._GENERIC_UNAVAILABLE_PHRASES)
            or any(pattern.search(normalized) for pattern in cls._UNAVAILABLE_HEDGE_PATTERNS)
            or bool(cls._NEITHER_EXPOSED_RE.search(normalized))
        )

    @classmethod
    def _sentence_mentions_tool(cls, normalized: str, name: str) -> bool:
        if not name:
            return False
        if re.search(rf"(?<![a-z0-9_-]){re.escape(name.lower())}(?![a-z0-9_-])", normalized):
            return True
        return any(alias in normalized for alias in cls._TOOL_ALIASES.get(name, ()))

    @classmethod
    def _registered_unavailable_tools(cls, result: Any, tools: list[dict[str, Any]]) -> list[str]:
        """Return registered tools the model falsely described as absent.

        Scoped per sentence, not across the whole response: a turn that
        answers an unrelated question and only in passing mentions a tool by
        name must not have its real answer treated as a false-availability
        claim just because some other sentence elsewhere says "unavailable."
        """
        data = result.data if isinstance(result.data, dict) else {}
        text = str(data.get("response") or result.text or "")
        called = {str(call.get("tool") or "") for call in (result.tool_calls or [])}
        found: list[str] = []
        for sentence in cls._split_sentences(text):
            normalized = cls._normalize(sentence)
            if not cls._sentence_claims_unavailability(normalized):
                continue
            for tool in tools:
                name = str(tool.get("name") or "")
                if name and name not in called and name not in found and cls._sentence_mentions_tool(normalized, name):
                    found.append(name)
        return found

    @classmethod
    def _strip_unavailable_claim_sentences(cls, text: str, offending_tools: list[str]) -> str:
        """Drop only the sentences that made the false claim, keeping any
        real answer the same response also contained."""
        kept: list[str] = []
        for sentence in cls._split_sentences(text):
            normalized = cls._normalize(sentence)
            if cls._sentence_claims_unavailability(normalized) and any(
                cls._sentence_mentions_tool(normalized, name) for name in offending_tools
            ):
                continue
            kept.append(sentence.strip())
        return " ".join(kept).strip()

    @classmethod
    def _unconfirmed_action_claims(cls, result: Any, tools: list[dict[str, Any]]) -> list[str]:
        """Return side-effecting tools the response claims to have already
        used when no matching tool call was actually made this turn — e.g.
        "test email sent" with no send_email call in the trace."""
        data = result.data if isinstance(result.data, dict) else {}
        text = str(data.get("response") or result.text or "")
        called = {str(call.get("tool") or "") for call in (result.tool_calls or [])}
        registered = {str(t.get("name") or "") for t in tools}
        claimed: list[str] = []
        for sentence in cls._split_sentences(text):
            normalized = cls._normalize(sentence)
            for tool_name, phrases in cls._ACTION_CLAIM_PHRASES.items():
                if tool_name in called or tool_name not in registered or tool_name in claimed:
                    continue
                if any(phrase in normalized for phrase in phrases):
                    claimed.append(tool_name)
        return claimed

    @classmethod
    def _strip_action_claim_sentences(cls, text: str, claimed_tools: list[str]) -> str:
        kept: list[str] = []
        for sentence in cls._split_sentences(text):
            normalized = cls._normalize(sentence)
            hit = any(
                any(phrase in normalized for phrase in cls._ACTION_CLAIM_PHRASES.get(name, ()))
                for name in claimed_tools
            )
            if hit:
                continue
            kept.append(sentence.strip())
        return " ".join(kept).strip()

    def _run_hallucination_recovery(
        self,
        *,
        result: Any,
        user_input: str,
        instruction: str,
        strip_fn: Callable[[str], str],
        correction: str,
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Any]],
        significance: str,
        provider: str | None,
        model: str | None,
        schema: dict[str, Any] | None,
        provider_error_mode: str,
        max_iterations: Any,
        extra_kwargs: dict[str, Any],
    ) -> Any:
        """Give the model one narrow follow-up turn to either make the real
        tool call or retract the claim; if it still doesn't call the tool,
        keep whatever of the original answer wasn't part of the false claim
        and append an honest correction rather than discarding the whole
        response."""
        recovery = self.complete_with_tools(
            user_input + "\n\n" + instruction,
            significance=significance,
            provider=provider,
            model=model,
            schema=schema,
            tools=tools,
            tool_handlers=tool_handlers,
            provider_error_mode=provider_error_mode,
            max_iterations=min(int(max_iterations), 3),
            **extra_kwargs,
        )
        if recovery.tool_calls:
            return recovery
        original_text = str(
            (result.data or {}).get("response") if isinstance(result.data, dict) else (result.text or "")
        )
        remaining = strip_fn(original_text)
        combined = (remaining + " " + correction).strip() if remaining else correction
        result.data = {
            **(result.data if isinstance(result.data, dict) else {}),
            "response": combined,
        }
        result.text = json.dumps(result.data, ensure_ascii=True)
        return result

    accepts_prose_finale = True

    def tool_limit_output(self, user_input: str, max_iterations: int, tool_log: list[dict[str, Any]], **kwargs: Any) -> str:
        done = ", ".join(dict.fromkeys(str(c.get("tool")) for c in tool_log)) or "none"
        note = (
            f"I ran out of steps before finishing — I used all {max_iterations} tool calls this turn "
            f"(tools used: {done}). Tell me to continue and I'll pick up from there, or raise "
            "conversation.max_tool_iterations if this kind of task needs more room."
        )
        return json.dumps({"response": note})

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        # Name what failed: "provider failure" is opaque; "my language model
        # timed out" tells the user it's transient and worth retrying.
        note = "My language model didn't respond just now — that's a transient hiccup, not your message. Say that again and I'll take another run at it."
        return json.dumps({"response": note})
