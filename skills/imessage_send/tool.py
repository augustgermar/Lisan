from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_imsg_common"))

from lisan_imsg import run_imsg  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "Error: text is required"
    to = str(args.get("to") or "").strip()
    chat_id = args.get("chat_id")
    if not to and chat_id is None:
        return "Error: pass a recipient ('to') or a chat_id from imessage_recent"
    cli = ["send", "--text", text]
    if chat_id is not None:
        try:
            cli += ["--chat-id", str(int(chat_id))]
        except (TypeError, ValueError):
            return "Error: chat_id must be an integer"
    else:
        cli += ["--to", to]
    ok, raw = run_imsg(cli, config, timeout=60.0)
    if not ok:
        return f"Error: {raw}"
    return json.dumps(
        {"status": "sent", "to": to or f"chat {chat_id}", "text": text},
        indent=2,
        ensure_ascii=False,
    )
