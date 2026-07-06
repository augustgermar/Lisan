from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..paths import repo_root
from ..tools.structured import extract_json
from .base import LLMResponse, ProviderClient, ProviderError


class CodexClient(ProviderClient):
    name = "codex"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
        working_directory: Path | None = None,
    ) -> LLMResponse:
        # The coding agent occasionally returns a truncated JSON response — the model
        # finishes mid-key when the streaming session is cut short by the
        # backend. The reply is unrecoverable, but a single fresh invocation
        # almost always succeeds. We retry once before surfacing the error.
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                return self._complete_once(
                    prompt=prompt, schema=schema, temperature=temperature,
                    agent=agent, significance=significance, model=model,
                    working_directory=working_directory,
                )
            except ProviderError as exc:
                if not _is_truncated_json_error(exc):
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ProviderError("coding agent retry loop exited without a response")

    def _complete_once(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        temperature: float,
        agent: str,
        significance: str,
        model: str | None,
        working_directory: Path | None,
    ) -> LLMResponse:
        binary = os.getenv(self.config["providers"]["codex"].get("binary_env") or "", "codex")
        chosen_model = model or self.config["providers"]["codex"].get("default_model") or None
        if not binary:
            raise ProviderError("CODEX_BIN is empty")

        env = os.environ.copy()
        home_dir = self.config.get("providers", {}).get("codex", {}).get("home_dir") or os.environ.get("LISAN_CODEX_HOME")
        if home_dir:
            env["HOME"] = str(home_dir)

        # Embed the schema as a prompt instruction instead of using --output-schema.
        # The --output-schema flag sends the schema to the OpenAI structured-output API,
        # which causes a 400 on models that don't support it (e.g. gpt-5.4-mini).
        full_prompt = prompt
        if schema:
            schema_instruction = (
                "\n\nRespond with valid JSON only — no prose, no code fences. "
                f"Your response must match this schema:\n{json.dumps(schema, indent=2)}"
            )
            full_prompt = prompt + schema_instruction

        output_path: Path | None = None
        try:
            args = [binary, "exec", "--skip-git-repo-check", "--cd", str(working_directory or repo_root())]
            if agent == "codex":
                # Owner decision 2026-07-06: the executor runs unsandboxed by
                # default ("--yolo") — scheduled tasks and plans kept failing
                # on network-dependent work (telegram sends, gmail, installs)
                # with misleading errors. The write-boundary briefing (never
                # touch files outside the Lisan install) remains in force at
                # the prompt layer; an owner who wants the cage back sets
                # codex.sandbox_mode in config ("workspace-write"/"read-only").
                mode = str(((self.config.get("providers") or {}).get("codex") or {}).get("sandbox_mode") or "danger-full-access")
                if mode == "danger-full-access":
                    args.append("--dangerously-bypass-approvals-and-sandbox")
                else:
                    args.extend(["--sandbox", mode])
            if agent != "codex":
                # Decision/extraction agents (listener, interlocutor, writer,
                # skeptic, ...) must never act on the system themselves — all
                # action flows through the run_codex tool and its approval
                # gate. The codex CLI is itself agentic and will sometimes run
                # commands inline despite prompt instructions, so the boundary
                # is enforced structurally: read-only sandbox for everyone but
                # the executor agent.
                args.extend(["--sandbox", "read-only"])
            if chosen_model:
                args.extend(["--model", chosen_model])

            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as output_file:
                output_path = Path(output_file.name)
            args.extend(["--output-last-message", str(output_path)])
            args.append("-")

            try:
                proc = subprocess.run(args, input=full_prompt, capture_output=True, text=True, env=env)
            except OSError as exc:
                # A missing/unlaunchable binary must surface as ProviderError so
                # callers deliver an honest failure message instead of silence
                # (raw FileNotFoundError bypasses the provider-failure path).
                raise ProviderError(f"coding agent binary {binary!r} could not be launched: {exc}") from exc
            if proc.returncode != 0:
                raise ProviderError(
                    "coding agent exec failed with exit code "
                    f"{proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
                )

            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                text = proc.stdout.strip()
            if schema:
                parsed = extract_json(text)
                if not isinstance(parsed, dict):
                    raise ProviderError(f"coding agent returned non-JSON: {text[:200]!r}")
                text = json.dumps(parsed, indent=2, ensure_ascii=True)
            return LLMResponse(
                text=text,
                provider=self.name,
                model=chosen_model or "",
                raw={"stdout": proc.stdout, "stderr": proc.stderr},
            )
        finally:
            if output_path and output_path.exists():
                output_path.unlink(missing_ok=True)


def _is_truncated_json_error(exc: ProviderError) -> bool:
    """True when the error message indicates the coding agent returned a truncated JSON stream."""
    message = str(exc)
    return "coding agent returned non-JSON" in message
