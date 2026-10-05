"""Claude provider: the local ``claude`` CLI driven through the Claude Agent SDK.

No API key and no HTTP endpoint: the SDK spawns the owner's logged-in Claude
Code binary, so tokens come from whatever that binary is signed into.

This is a *completion* provider, not an agent. Every call is one turn, no
tools, an empty scratch directory, no user/project settings (so no CLAUDE.md,
hooks, skills, or auto-memory leak into a Lisan prompt), and no session
transcript written to ``~/.claude``. That is deliberately narrower than the
codex provider, which runs every agent under ``all_agents_sandbox_mode``.
Tools are opt-in per install via ``providers.claude.allowed_tools``. The
executor and delegation paths construct ``CodexClient`` directly and are not
affected by this provider.

``temperature`` is accepted for interface parity and ignored: the SDK does not
expose it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..tools.structured import extract_json
from .base import LLMResponse, ProviderClient, ProviderError
from .codex import _is_schema_echo

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_TIMEOUT_SECONDS = 300.0
SYSTEM_PROMPT = (
    "You are the text-generation backend for another program. Answer the "
    "message exactly as instructed, with no preamble. You have no tools and "
    "cannot read or change files."
)


class ClaudeClient(ProviderClient):
    name = "claude"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise ProviderError(
                "claude provider needs the Claude Agent SDK: pip install claude-agent-sdk"
            ) from exc

        cfg = (self.config.get("providers") or {}).get("claude") or {}
        chosen_model = model or cfg.get("default_model") or DEFAULT_MODEL
        timeout = float(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

        full_prompt = prompt
        if schema:
            full_prompt = (
                prompt
                + "\n\nRespond with valid JSON only — no prose, no code fences. "
                f"Your response must match this schema:\n{json.dumps(schema, indent=2)}"
            )

        text = _run_sync(self._query(full_prompt, chosen_model, cfg), timeout)
        if schema:
            parsed = extract_json(text)
            if not isinstance(parsed, dict):
                raise ProviderError(f"claude returned non-JSON: {text[:200]!r}")
            if _is_schema_echo(parsed):
                raise ProviderError("claude returned the schema definition instead of a response instance")
            text = json.dumps(parsed, indent=2, ensure_ascii=True)
        return LLMResponse(text=text, provider=self.name, model=chosen_model)

    async def _query(self, prompt: str, model: str, cfg: dict[str, Any]) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            CLINotFoundError,
            ProcessError,
            ResultMessage,
            TextBlock,
            query,
        )

        allowed = list(cfg.get("allowed_tools") or [])
        binary = os.environ.get(cfg.get("binary_env") or "CLAUDE_BIN") or shutil.which("claude")
        scratch = Path(tempfile.mkdtemp(prefix="lisan-claude-"))
        try:
            options = ClaudeAgentOptions(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                tools=allowed,
                allowed_tools=allowed,
                max_turns=int(cfg.get("max_turns") or (1 if not allowed else 20)),
                setting_sources=[],
                cwd=str(scratch),
                cli_path=binary or None,
                extra_args={"no-session-persistence": None},
            )
            chunks: list[str] = []
            result_text: str | None = None
            try:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        chunks.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        if message.is_error:
                            detail = message.result or "; ".join(message.errors or []) or message.subtype
                            raise ProviderError(f"claude call failed: {detail}")
                        result_text = message.result
            except CLINotFoundError as exc:
                raise ProviderError(f"claude binary could not be found or launched: {exc}") from exc
            except ProcessError as exc:
                raise ProviderError(f"claude process failed: {exc}") from exc
            text = (result_text if result_text else "".join(chunks)).strip()
            if not text:
                raise ProviderError("claude returned an empty response")
            return text
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _run_sync(coro, timeout: float) -> str:
    """Run a coroutine to completion from sync code, with a wall-clock limit.

    Always uses a private event loop on a worker thread, so it behaves the same
    whether or not the caller (Telegram bridge, jobs runner) already has a loop.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(asyncio.wait_for(coro, timeout))
        except asyncio.TimeoutError:
            box["error"] = ProviderError(f"claude call timed out after {timeout:.0f}s")
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=runner, name="lisan-claude", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
