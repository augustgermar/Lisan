from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_imsg_common"))

from lisan_imsg import parse_ndjson, run_imsg, trim_chat  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    limit = min(max(int(args.get("limit") or 10), 1), 50)
    ok, raw = run_imsg(["chats", "--limit", str(limit)], config)
    if not ok:
        return f"Error: {raw}"
    chats = [trim_chat(c) for c in parse_ndjson(raw)]
    if not chats:
        return "No conversations found."
    return json.dumps(chats, indent=2, ensure_ascii=False)
