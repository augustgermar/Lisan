from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import repo_root, skills_root
from ..providers.codex import CodexClient
from ..tools.assembler import assemble_context
from ..tools.skill_loader import load_skill_handlers
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
        "description": "Delegate a coding, system administration, or file-editing task to the codex agent. Codex can read/write files, run commands, and fix errors. Describe the task clearly; codex executes and returns the result. Requires user approval for write operations.",
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
        "run_codex": lambda task, working_directory="~": run_codex(
            task,
            working_directory=working_directory,
            vault=vault,
            config=config,
            db_path=db_path,
            approval_fn=approval_fn,
        ),
    }
    handlers.update(load_skill_handlers(skills_root(), vault=vault, config=config or load_config()))
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


def run_codex(
    task: str,
    *,
    working_directory: str = "~",
    vault: Path,
    config: dict[str, Any] | None = None,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    approval_fn: Callable[[str, dict[str, Any]], bool] | None = None,
) -> str:
    config = config or load_config()
    approval_fn = approval_fn or _approve_action

    wd = Path(working_directory).expanduser()
    if not wd.is_absolute():
        wd = repo_root()

    approved = approval_fn("run_codex", {"task": task, "working_directory": str(wd)})
    if not approved:
        return "User denied the task"

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


def _codex_default_model(config: dict[str, Any], provider: str | None = None) -> str | None:
    codex = config.get("providers", {}).get("codex", {})
    model = codex.get("default_model")
    return str(model) if model else None


def _build_codex_prompt(*, task: str, working_directory: Path, vault: Path, db_path: Path | None) -> str:
    context = assemble_context(task, vault=vault, db_path=db_path)
    return (
        "You are Codex executing a task for the Lisan memory system.\n\n"
        f"Working directory: {working_directory}\n\n"
        f"Task:\n{task}\n\n"
        "Relevant memory context:\n"
        f"{context}\n\n"
        "Execute the task directly and return only the result of your work."
    )


def _approve_action(tool_name: str, args: dict[str, Any]) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"[self] I'd like to run codex to: {args.get('task', '')}")
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
