from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import repo_root, skills_root
from ..providers.codex import CodexClient
from .assembler import assemble_context
from .skill_loader import load_skill_handlers
from .structured import extract_json


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_memory",
        "description": "Search your own memory vault for relevant records. Use when you need context the current conversation hasn't provided.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file on the local filesystem. Use to inspect configuration, code, documents, or any text file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_codex",
        "description": "Delegate a coding, system administration, or file-editing task to the codex agent. Codex can read/write files, run shell commands, run Lisan CLI commands, and fix errors. Describe the task clearly; codex executes and returns the result. Requires user approval for write operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What codex should do"},
                "working_directory": {
                    "type": "string",
                    "description": "Directory codex should work in",
                    "default": "~",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "self_state",
        "description": (
            "Your own live operational state: job queue counts, next scheduled task, index size, "
            "last dreamer/analyst runs, whether your services are up, recent errors. ALWAYS use "
            "this to answer questions about your own state, queue, schedule, or health — never "
            "answer those from memory."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_plan",
        "description": (
            "Turn a multi-step goal into a durable background plan that executes step by step "
            "and reports back when done — use this when a request needs several actions that "
            "take time (inspect, then process, then verify), not for a single immediate action. "
            "Each step has a kind: 'codex' (run a shell/CLI/file task — the workhorse), 'prompt' "
            "(run a prompt through your own pipeline), or 'note' (record an observation). Steps "
            "run in order; each sees the goal and the results of earlier steps. The user approves "
            "the plan now, at creation. Keep plans to a few concrete steps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the plan achieves, in one sentence"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["codex", "prompt", "note"]},
                            "description": {"type": "string"},
                        },
                        "required": ["kind", "description"],
                    },
                },
            },
            "required": ["goal", "steps"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedule something to happen at a future time. Kinds: 'reminder' sends the user a "
            "message at that time; 'prompt' runs a prompt through your own pipeline at that time "
            "and sends the user the result; 'codex' runs a codex task at that time (the user "
            "approves it now, at scheduling time). 'when' must be deterministic: 'YYYY-MM-DD HH:MM' "
            "(user's local time), 'HH:MM' (next such time), 'tomorrow HH:MM', or a relative offset "
            "like '+30m', '+2h', '+3d'. Never pass fuzzy phrases like 'next thursday' — resolve them "
            "to a date first; if you are unsure of today's date, prefer a relative offset (error "
            "messages include the current local time, so you can correct yourself). Optional "
            "'recurrence': 'every:30m', 'every:2h', 'every:1d', or 'daily@HH:MM'. Omit 'when' on "
            "recurring tasks to start at the next occurrence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The reminder message, prompt, or codex task"},
                "when": {"type": "string", "description": "When to fire (deterministic forms only)"},
                "kind": {"type": "string", "enum": ["reminder", "prompt", "codex"], "default": "reminder"},
                "recurrence": {"type": "string", "description": "Optional recurrence rule"},
            },
            "required": ["text"],
        },
    },
]


def build_tool_handlers(
    *,
    vault: Path,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    domain: str | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Callable[..., str]]:
    handlers: dict[str, Callable[..., str]] = {
        "search_memory": lambda query: search_memory(
            query,
            vault=vault,
            db_path=db_path,
            conversation_id=conversation_id,
            domain=domain,
        ),
        "read_file": read_file,
        "run_codex": lambda task, working_directory=None: run_codex(
            task,
            working_directory=working_directory,
            vault=vault,
            config=config,
            db_path=db_path,
            approval_fn=approval_fn,
        ),
        "self_state": lambda: self_state(vault=vault, db_path=db_path),
        "create_plan": lambda goal, steps: create_plan_tool(
            goal=goal,
            steps=steps,
            db_path=db_path,
            conversation_id=conversation_id,
            approval_fn=approval_fn,
        ),
        "schedule_task": lambda text, when=None, kind="reminder", recurrence=None: schedule_task_tool(
            text=text,
            when=when,
            kind=kind,
            recurrence=recurrence,
            db_path=db_path,
            conversation_id=conversation_id,
            approval_fn=approval_fn,
        ),
    }
    handlers.update(
        load_skill_handlers(
            skills_root(),
            vault=vault,
            config=config or load_config(),
            approval_fn=approval_fn or _approve_action,
        )
    )
    return handlers


def search_memory(
    query: str,
    *,
    vault: Path,
    db_path: Path | None = None,
    conversation_id: str | None = None,
    domain: str | None = None,
) -> str:
    return assemble_context(
        query,
        vault=vault,
        db_path=db_path,
        conversation_id=conversation_id,
        domain=domain,
    )


def self_state(*, vault: Path, db_path: Path | None = None) -> str:
    from .self_model import render_self_state, snapshot_self_state

    try:
        return render_self_state(snapshot_self_state(vault=vault, db_path=db_path))
    except Exception as exc:
        return f"Error: could not read own state: {exc}"


def read_file(path: str, *, max_bytes: int = 50 * 1024) -> str:
    file_path = Path(path)
    if not file_path.is_absolute():
        return f"Error: path must be absolute: {path!r}"
    if not file_path.exists():
        return f"Error: file does not exist: {file_path}"
    if not file_path.is_file():
        return f"Error: not a regular file: {file_path}"
    size = file_path.stat().st_size
    if size > max_bytes:
        return f"Error: file exceeds size limit of {max_bytes} bytes: {file_path} ({size} bytes)"
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: file is not valid UTF-8 text: {file_path}"
    except Exception as exc:
        return f"Error: failed to read {file_path}: {exc}"


def codex_workspace() -> str:
    """The executor's default workspace: the smallest directory containing
    both the repo and the vault. Everything outside it is read-only to the
    executor by sandbox policy — so when repo and vault share no ancestor
    deeper than the user's home (disjoint trees give a common path of
    home, /Users, or /), the boundary must collapse tighter, not wider:
    the workspace falls back to the repo alone."""
    import os

    from ..paths import vault_root

    try:
        common = Path(os.path.commonpath([str(repo_root()), str(vault_root())]))
    except ValueError:
        return str(repo_root())
    home = Path.home()
    if common == home or common in home.parents or common == Path(common.anchor):
        return str(repo_root())
    return str(common)


def run_codex(
    task: str,
    *,
    working_directory: str | None = None,
    vault: Path,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> str:
    config = config or load_config()
    approval_fn = approval_fn or _approve_action

    wd = Path(working_directory).expanduser() if working_directory else Path(codex_workspace())
    if not wd.is_absolute():
        wd = repo_root()

    approved = approval_fn("run_codex", {"task": task, "working_directory": str(wd)})
    if not approved:
        return (
            "Approval was not granted, so I did not run this. On Telegram I ask for approval "
            "with a message you answer 'yes' to; in the CLI I prompt interactively. This is the "
            "approval gate — not a permissions or system error."
        )

    prompt = _build_codex_prompt(task=task, working_directory=wd, vault=vault, db_path=db_path)
    try:
        client = CodexClient(config)
        response = client.complete(
            prompt,
            agent="codex",
            significance="medium",
            model=model or _codex_default_model(config, provider),
            working_directory=wd,
        )
        return response.text.strip()
    except Exception as exc:
        return str(exc)


_TELEGRAM_CONVERSATION_RE = re.compile(r"^telegram-(\d+)\b")


def create_plan_tool(
    *,
    goal: str,
    steps: list[dict[str, str]],
    db_path: Path | None = None,
    conversation_id: str | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> str:
    """Conversational plan creation. The approval here covers every codex
    step — the plan runs unattended, so creation is the only veto point."""
    from .plans import create_plan

    if not isinstance(steps, list):
        return "Error: steps must be a list of {kind, description} objects"
    if any(str(s.get("kind") or "codex").lower() == "codex" for s in steps if isinstance(s, dict)):
        rendered = "; ".join(str(s.get("description") or "") for s in steps if isinstance(s, dict))
        approved = (approval_fn or _approve_action)("create_plan", {"task": f"{goal} — steps: {rendered}"})
        if not approved:
            return "User denied the plan"

    chat_id: int | None = None
    match = _TELEGRAM_CONVERSATION_RE.match(str(conversation_id or ""))
    if match:
        chat_id = int(match.group(1))
    try:
        summary = create_plan(
            goal=goal,
            steps=steps,
            chat_id=chat_id,
            conversation_id=conversation_id,
            db_path=db_path,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    return (
        f"Plan created ({summary['plan_id']}): {summary['goal']} — {summary['steps']} step(s). "
        "It runs in the background; I'll report when it finishes."
    )


def schedule_task_tool(
    *,
    text: str,
    when: str | None = None,
    kind: str = "reminder",
    recurrence: str | None = None,
    db_path: Path | None = None,
    conversation_id: str | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> str:
    """Conversational entry point for scheduling. Codex tasks get the approval
    gate *now* — the future firing runs unattended, so scheduling is the only
    moment the owner can say no."""
    from .scheduler import schedule_task

    if str(kind).strip().lower() == "codex":
        approved = (approval_fn or _approve_action)(
            "schedule_task", {"task": text, "when": str(when or recurrence or "")}
        )
        if not approved:
            return "User denied scheduling the task"

    chat_id: int | None = None
    match = _TELEGRAM_CONVERSATION_RE.match(str(conversation_id or ""))
    if match:
        chat_id = int(match.group(1))

    try:
        summary = schedule_task(
            kind=kind,
            text=text,
            when=when,
            recurrence=recurrence,
            chat_id=chat_id,
            conversation_id=conversation_id,
            db_path=db_path,
        )
    except ValueError as exc:
        return f"Error: {exc}"
    recur_note = f", recurring {summary['recurrence']}" if summary.get("recurrence") else ""
    return (
        f"Scheduled {summary['kind']} for {summary['scheduled_for_local']}{recur_note} "
        f"(task id {summary['job_id']})"
    )


def _codex_default_model(config: dict[str, Any], provider: str | None = None) -> str | None:
    codex = config.get("providers", {}).get("codex", {})
    model = codex.get("default_model")
    return str(model) if model else None


def _build_codex_prompt(*, task: str, working_directory: Path, vault: Path, db_path: Path | None) -> str:
    from .self_model import cli_reference

    context = assemble_context(task, vault=vault, db_path=db_path)
    return (
        "You are Codex executing a task for the Lisan memory system.\n\n"
        f"Working directory: {working_directory}\n\n"
        f"Task:\n{task}\n\n"
        "HARD WRITE BOUNDARY: you may create or modify files ONLY inside the Lisan "
        "install (its repo, vault, and database). Everything else on this machine — "
        "including the user's Obsidian vault and personal documents — is strictly "
        "READ-ONLY source material, even when a task sounds like it wants an edit "
        "there. Memory updates always mean Lisan's own records, never the source "
        "notes they came from. One exception inside the install: "
        "primer/identity-core.md is the identity kernel and is READ-ONLY too — "
        "it changes only through a ratification ceremony or the owner's own hand.\n\n"
        "Lisan's own CLI is available to you and is usually the right way to act on "
        "Lisan's memory (ingesting files, running jobs, checking health):\n"
        f"{cli_reference()}\n\n"
        "Relevant memory context:\n"
        f"{context}\n\n"
        "Execute the task directly and return only the result of your work."
    )


def _approve_action(tool_name: str, args: dict[str, Any]) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"[self] I'd like to run {tool_name}: {args.get('task', '')}")
    if args.get("working_directory"):
        print(f"Working directory: {args.get('working_directory', '')}")
    while True:
        answer = input("[approve / deny / modify]: ").strip().lower()
        if answer in {"approve", "yes", "y"}:
            return True
        if answer in {"deny", "no", "n", ""}:
            return False
        if answer.startswith("modify "):
            args["task"] = answer.removeprefix("modify ").strip()
            if args["task"]:
                return True
            return False


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for block in _tool_call_blocks(text):
        parsed = extract_json(block)
        if isinstance(parsed, dict):
            calls.extend(_normalize_tool_calls(parsed))
    parsed = extract_json(text)
    if isinstance(parsed, dict):
        calls.extend(_normalize_tool_calls(parsed))
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                calls.extend(_normalize_tool_calls(item))
    return _dedupe_calls(calls)


def _tool_call_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", text, flags=re.DOTALL | re.IGNORECASE):
        blocks.append(match.group(1).strip())
    return blocks


def _normalize_tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if isinstance(data.get("tool_calls"), list):
        for item in data["tool_calls"]:
            if isinstance(item, dict):
                calls.extend(_normalize_tool_calls(item))
        return calls
    tool_name = data.get("tool") or data.get("name")
    if not tool_name:
        return calls
    args = data.get("args") or data.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    calls.append({"tool": str(tool_name), "args": args})
    return calls


def _dedupe_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for call in calls:
        key = (str(call.get("tool") or ""), json.dumps(call.get("args") or {}, sort_keys=True))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        unique.append(call)
    return unique
