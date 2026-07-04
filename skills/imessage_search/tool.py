from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_imsg_common"))

from lisan_imsg import parse_ndjson, run_imsg, trim_message  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: query is required"
    limit = min(max(int(args.get("limit") or 20), 1), 100)
    ok, raw = run_imsg(["search", "--query", query, "--limit", str(limit)], config)
    if not ok:
        return f"Error: {raw}"
    messages = [trim_message(m) for m in parse_ndjson(raw)]
    if not messages:
        return f"No messages matched {query!r}."
    return json.dumps(messages, indent=2, ensure_ascii=False)
