"""Shared helper for Lisan imessage skills: runs the external `imsg` CLI
(https://github.com/steipete/imsg — `brew install steipete/tap/imsg`) and
parses its NDJSON output. Standard library only.

Binary resolution: ``LISAN_IMSG_BIN`` env var → ``skills.imessage.binary`` in
config.json → ``imsg`` on PATH.

Reading history requires Full Disk Access for the terminal/app running Lisan
(imsg reads ~/Library/Messages/chat.db); sending requires Automation
permission for Messages.app. Errors from either surface as readable strings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

NOT_INSTALLED = (
    "The `imsg` CLI is not installed. Install it with: brew install steipete/tap/imsg\n"
    "Reading messages also needs Full Disk Access for the terminal running Lisan "
    "(System Settings → Privacy & Security → Full Disk Access)."
)


def imsg_binary(config: dict[str, Any] | None = None) -> str | None:
    env = os.environ.get("LISAN_IMSG_BIN")
    if env:
        return env
    if config:
        configured = (config.get("skills") or {}).get("imessage", {}).get("binary")
        if configured:
            return str(configured)
    return shutil.which("imsg")


def run_imsg(
    cli_args: list[str],
    config: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Run imsg with --json and return (ok, raw stdout or error text)."""
    binary = imsg_binary(config)
    if not binary:
        return False, NOT_INSTALLED
    try:
        result = subprocess.run(
            [binary, *cli_args, "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, NOT_INSTALLED
    except subprocess.TimeoutExpired:
        return False, f"imsg timed out after {timeout:.0f}s"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return False, f"imsg exited {result.returncode}: {detail or 'no output'}"
    return True, result.stdout


def parse_ndjson(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def trim_chat(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "chat_id": chat.get("id"),
        "identifier": chat.get("identifier", ""),
        "name": chat.get("display_name") or chat.get("name") or "",
        "participants": chat.get("participants", []),
        "service": chat.get("service", ""),
        "is_group": chat.get("is_group", False),
        "last_message_at": chat.get("last_message_at", ""),
    }


def trim_message(msg: dict[str, Any]) -> dict[str, Any]:
    trimmed = {
        "id": msg.get("id"),
        "chat_id": msg.get("chat_id"),
        "chat": msg.get("chat_name") or msg.get("chat_identifier") or "",
        "sender": msg.get("sender", ""),
        "is_from_me": msg.get("is_from_me", False),
        "text": msg.get("text", ""),
        "created_at": msg.get("created_at", ""),
    }
    if msg.get("reply_to_text"):
        trimmed["in_reply_to"] = msg["reply_to_text"]
    if msg.get("attachments"):
        trimmed["attachments"] = len(msg["attachments"])
    return trimmed
