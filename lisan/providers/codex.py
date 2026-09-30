from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..paths import repo_root
from ..tools.structured import extract_json
from ..tools.tracing import progress_listener_active, record_codex_progress
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
        started = time.monotonic()
        try:
            args = [binary, "exec", "--skip-git-repo-check", "--cd", str(working_directory or repo_root())]
            codex_config = (self.config.get("providers") or {}).get("codex") or {}
            mode = _resolve_sandbox_mode(agent, codex_config)
            if mode == "danger-full-access":
                args.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                args.extend(["--sandbox", mode])
            if chosen_model:
                args.extend(["--model", chosen_model])

            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as output_file:
                output_path = Path(output_file.name)
            args.extend(["--output-last-message", str(output_path)])
            args.append("-")

            try:
                record_codex_progress(
                    "start",
                    agent=agent,
                    model=chosen_model or "",
                    working_directory=str(working_directory or repo_root()),
                )
                if progress_listener_active():
                    proc = _run_codex_process(
                        args,
                        prompt=full_prompt,
                        env=env,
                        agent=agent,
                        model=chosen_model or "",
                        working_directory=working_directory or repo_root(),
                        started=started,
                    )
                else:
                    proc = subprocess.run(
                        args,
                        input=full_prompt,
                        capture_output=True,
                        text=True,
                        env=env,
                    )
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
                if _is_schema_echo(parsed):
                    raise ProviderError(
                        "coding agent returned the schema definition instead of a response instance"
                    )
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


def _resolve_sandbox_mode(agent: str, codex_config: dict[str, Any]) -> str:
    """The cPanel/codex sandbox mode for one agent's subprocess: read-only,
    workspace-write, or danger-full-access.

    Precedence, most to least specific:

    1. ``codex.sandbox_mode_by_agent.<agent>`` — an explicit setting for this
       exact agent name (``codex``, ``interlocutor``, ``writer``, ``skeptic``,
       ``listener``, ...). This is the dial the owner turns per-agent.
    2. ``codex.sandbox_mode`` — legacy setting, but ONLY for ``agent == "codex"``
       (the execute_task executor). Kept for backward compatibility with
       installs from before per-agent config existed.
    3. ``codex.all_agents_sandbox_mode`` — the blanket setting for every agent
       that has no more specific entry. Historically this was the only knob
       decision/extraction agents (interlocutor, writer, skeptic, ...) had;
       it remains the fallback so an unconfigured install keeps working
       exactly as before.
    4. Hard default: ``danger-full-access`` for the executor (``codex``),
       ``read-only`` for everyone else — the safety boundary this file has
       always documented: the codex CLI is itself agentic and will sometimes
       run commands inline despite prompt instructions, so non-executor
       agents are sandboxed structurally, not just by the prompt asking
       nicely.
    """
    by_agent = codex_config.get("sandbox_mode_by_agent")
    if isinstance(by_agent, dict) and agent in by_agent:
        explicit = str(by_agent.get(agent) or "").strip()
        if explicit:
            return explicit

    if agent == "codex":
        return str(codex_config.get("sandbox_mode") or "danger-full-access")

    return str(codex_config.get("all_agents_sandbox_mode") or "read-only")


def _is_truncated_json_error(exc: ProviderError) -> bool:
    """True when the error message indicates the coding agent returned a truncated JSON stream."""
    message = str(exc)
    return (
        "coding agent returned non-JSON" in message
        or "returned the schema definition instead of a response instance" in message
    )


def _run_codex_process(
    args: list[str],
    *,
    prompt: str,
    env: dict[str, str],
    agent: str,
    model: str,
    working_directory: Path,
    started: float,
) -> subprocess.CompletedProcess[str]:
    """Run Codex while publishing bounded, non-sensitive progress events."""
    # JSONL exposes safe lifecycle events (command started/completed, turn
    # phases) without requiring us to scrape an interactive terminal. It is
    # used only for the live progress path; the normal batch path is unchanged.
    stream_args = list(args)
    if "--json" not in stream_args:
        stream_args.append("--json")
    proc = subprocess.Popen(
        stream_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()

    selector = selectors.DefaultSelector()
    streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
    if proc.stdout is not None:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    if proc.stderr is not None:
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")

    # Heartbeats only fill silences: nothing is printed while Codex is
    # emitting events, and a quiet stretch names the command still running.
    last_event = started
    last_heartbeat = started
    running: dict[str, str] = {}
    try:
        while selector.get_map():
            now = time.monotonic()
            elapsed_ms = int((now - started) * 1000)
            if now - last_event >= _HEARTBEAT_QUIET_S and now - last_heartbeat >= _HEARTBEAT_QUIET_S:
                command = next(reversed(running.values()), "")
                record_codex_progress(
                    "heartbeat",
                    agent=agent,
                    model=model,
                    working_directory=str(working_directory),
                    elapsed_ms=elapsed_ms,
                    detail=f"still running: {_clip(command.split(chr(10), 1)[0], 100)}" if command else "",
                )
                last_heartbeat = now

            ready = selector.select(timeout=0.5)
            for key, _ in ready:
                line = key.fileobj.readline()
                if line == "":
                    selector.unregister(key.fileobj)
                    continue
                stream = str(key.data)
                streams[stream].append(line)
                if stream == "stdout":
                    payload = _record_codex_json_progress(
                        line,
                        agent=agent,
                        model=model,
                        working_directory=working_directory,
                        elapsed_ms=elapsed_ms,
                    )
                    if payload:
                        last_event = now
                        if payload.get("type") == "command":
                            command_id = str(payload.get("id") or payload.get("command"))
                            if payload.get("phase") == "started":
                                running[command_id] = str(payload.get("command") or "")
                            else:
                                running.pop(command_id, None)
                else:
                    detail = _safe_codex_progress_line(line)
                    if detail:
                        last_event = now
                        record_codex_progress(
                            "detail",
                            agent=agent,
                            model=model,
                            working_directory=str(working_directory),
                            elapsed_ms=elapsed_ms,
                            detail=detail,
                        )
    finally:
        selector.close()

    returncode = proc.wait()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if returncode:
        record_codex_progress(
            "failed",
            agent=agent,
            model=model,
            working_directory=str(working_directory),
            elapsed_ms=elapsed_ms,
        )
    return subprocess.CompletedProcess(
        args=stream_args,
        returncode=returncode,
        stdout="".join(streams["stdout"]),
        stderr="".join(streams["stderr"]),
    )


_HEARTBEAT_QUIET_S = 10
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SENSITIVE_RE = re.compile(r"(?:authorization|bearer|api[_ -]?key|password|secret|token)", re.I)

# Live progress is ephemeral (never persisted), so it can carry commands,
# output, and Codex's own text; credentials inside them are masked in place
# rather than hiding the whole line.
_MASK = "****"
_SECRET_NAME = r"[\w.-]*(?:passw(?:or)?d|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?)[\w.-]*"
_AUTH_SCHEMES = r"(?:bearer|basic|token|digest)"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
        f"-----PRIVATE KEY {_MASK}-----",
    ),
    (re.compile(rf"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:{_AUTH_SCHEMES}\s+)?)[^\s\"',;]+"), rf"\1{_MASK}"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1{_MASK}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), _MASK),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), _MASK),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), _MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), _MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _MASK),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@"), rf"\1{_MASK}@"),
    # --password value, --api-key=value
    (re.compile(rf"(?i)((?:^|\s)--?{_SECRET_NAME}(?:=|\s+))(?!-)[\"']?[^\s\"']+[\"']?"), rf"\1{_MASK}"),
    # NAME=value, NAME: value, "name": "value"
    (
        re.compile(
            rf"(?i)(\b{_SECRET_NAME}[\"']?\s*[:=]\s*)(?!{re.escape(_MASK)}|{_AUTH_SCHEMES}\s)[\"']?[^\s\"',;&)}}]+[\"']?"
        ),
        rf"\1{_MASK}",
    ),
)
_SHELL_WRAPPER_RE = re.compile(r"^(?:\S*/)?(?:bash|zsh|sh|dash)\s+-l?c\s+(['\"]?)(.*)\1\s*$", re.S)
_OUTPUT_TAIL_LINES = 40
_LINE_CAP = 400
_TEXT_CAP = 6000


def _mask_secrets(text: str) -> str:
    """Replace credential values with a mask, keeping the surrounding text."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clean_text(text: str, limit: int = _TEXT_CAP) -> str:
    cleaned = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return _clip(_mask_secrets(cleaned), limit)


def _display_command(command: str) -> str:
    """Strip Codex's `bash -lc '...'` wrapper and mask credentials."""
    cleaned = _ANSI_RE.sub("", command).strip()
    match = _SHELL_WRAPPER_RE.match(cleaned)
    if match:
        quote, inner = match.groups()
        cleaned = inner.replace("'\\''", "'") if quote == "'" else inner.replace('\\"', '"')
    return _clip(_mask_secrets(cleaned.strip()), 2000)


def _output_tail(output: str) -> tuple[list[str], int]:
    """Last lines of command output (masked, clipped) plus the total count."""
    cleaned = _mask_secrets(_ANSI_RE.sub("", output).replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return [_clip(line, _LINE_CAP) for line in lines[-_OUTPUT_TAIL_LINES:]], len(lines)


def _safe_codex_progress_line(line: str) -> str:
    """Keep useful CLI status text while rejecting likely sensitive output."""
    cleaned = _ANSI_RE.sub("", line).strip()
    if not cleaned or len(cleaned) > 180 or _SENSITIVE_RE.search(cleaned):
        return ""
    if cleaned.startswith(("{", "[", "```")):
        return ""
    # `codex exec` may echo the complete delegated prompt to stderr. Only
    # surface its fixed runtime banner; conversation text, retrieved memory,
    # and tool schemas stay hidden from the live activity feed.
    safe_prefixes = (
        "OpenAI Codex v",
        "workdir:",
        "model:",
        "provider:",
        "approval:",
        "sandbox:",
        "reasoning effort:",
        "reasoning summaries:",
    )
    if not cleaned.startswith(safe_prefixes):
        return ""
    return cleaned


def _codex_json_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """Map one `codex exec --json` event to a structured, masked payload."""
    event_type = str(event.get("type") or "")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    phase = event_type.split(".", 1)[1] if event_type.startswith("item.") else ""

    if event_type == "turn.started":
        return {"type": "turn", "phase": "started"}
    if event_type == "turn.completed":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        return {"type": "turn", "phase": "completed", "usage": {k: v for k, v in usage.items() if isinstance(v, int)}}
    if event_type == "turn.failed":
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        return {"type": "turn", "phase": "failed", "message": _clean_text(str(error.get("message") or ""), 1000)}
    if event_type == "error":
        return {"type": "error", "message": _clean_text(str(event.get("message") or ""), 1000)}
    if not phase or not item_type:
        return None

    if item_type == "command_execution":
        payload: dict[str, Any] = {
            "type": "command",
            "phase": phase,
            "id": str(item.get("id") or ""),
            "command": _display_command(str(item.get("command") or "")),
        }
        if phase == "completed":
            tail, total = _output_tail(str(item.get("aggregated_output") or ""))
            payload.update(
                exit_code=item.get("exit_code") if isinstance(item.get("exit_code"), int) else None,
                status=str(item.get("status") or ""),
                output_tail=tail,
                output_lines=total,
            )
        elif phase == "updated":
            return None
        return payload
    if item_type in {"file_change", "patch_apply"}:
        if phase != "completed":
            return None
        changes = []
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                continue
            kind = change.get("kind")
            if isinstance(kind, dict):
                kind = kind.get("type")
            changes.append({"path": _clip(str(change.get("path") or "?"), 300), "kind": str(kind or "update")})
        return {"type": "file_change", "changes": changes, "status": str(item.get("status") or "")}
    if item_type in {"reasoning", "agent_message"}:
        text = _clean_text(str(item.get("text") or ""))
        if phase != "completed" or not text:
            return None
        return {"type": "reasoning" if item_type == "reasoning" else "message", "text": text}
    if item_type == "mcp_tool_call":
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        return {
            "type": "tool",
            "phase": phase,
            "name": ".".join(str(part) for part in (item.get("server"), item.get("tool")) if part),
            "status": str(item.get("status") or ""),
            "error": _clean_text(str(error.get("message") or ""), 500),
        } if phase != "updated" else None
    if item_type == "web_search":
        query = _clean_text(str(item.get("query") or ""), 300)
        return {"type": "web_search", "query": query} if phase == "completed" and query else None
    if item_type == "todo_list":
        items = [
            {"text": _clean_text(str(entry.get("text") or ""), 300), "done": bool(entry.get("completed"))}
            for entry in item.get("items") or []
            if isinstance(entry, dict)
        ]
        return {"type": "todo", "phase": phase, "items": items}
    if item_type == "error":
        return {"type": "error", "message": _clean_text(str(item.get("message") or ""), 1000)}
    if phase == "started":
        return {"type": "other", "item_type": item_type}
    return None


def _payload_summary(payload: dict[str, Any]) -> str:
    """One-line description of a payload, used as the event's `detail`."""
    kind = payload.get("type")
    if kind == "command":
        command = _clip(str(payload.get("command") or "").split("\n", 1)[0], 120)
        if payload.get("phase") == "started":
            return f"running command: {command}"
        exit_code = payload.get("exit_code")
        return f"command finished: {command}" + (f" (exit {exit_code})" if exit_code is not None else "")
    if kind == "file_change":
        return f"file changes completed ({len(payload.get('changes') or [])} change(s))"
    if kind == "turn":
        return f"Codex turn {payload.get('phase')}"
    if kind == "reasoning":
        return "reasoning"
    if kind == "message":
        return "Codex produced a response"
    if kind == "tool":
        return f"tool {payload.get('phase')}: {payload.get('name')}"
    if kind == "error":
        return f"error: {_clip(str(payload.get('message') or ''), 120)}"
    return str(kind or "activity")


def _record_codex_json_progress(
    line: str,
    *,
    agent: str,
    model: str,
    working_directory: Path,
    elapsed_ms: int,
) -> dict[str, Any] | None:
    """Publish one Codex JSONL event as a live `item` progress event."""
    try:
        event = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    payload = _codex_json_payload(event)
    if payload is None:
        return None
    record_codex_progress(
        "item", agent=agent, model=model,
        working_directory=str(working_directory), elapsed_ms=elapsed_ms,
        detail=_payload_summary(payload), payload=payload,
    )
    return payload


def _is_schema_echo(value: dict[str, Any]) -> bool:
    """Detect Codex emitting the supplied JSON Schema instead of its instance."""
    return (
        isinstance(value.get("$schema"), str)
        and value.get("type") == "object"
        and isinstance(value.get("properties"), dict)
    )
