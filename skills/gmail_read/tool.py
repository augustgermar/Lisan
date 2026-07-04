from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_google_common"))

from lisan_google import (  # noqa: E402
    GMAIL_API,
    GoogleAuthError,
    api_request,
    extract_body,
    message_summary,
)


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        return "Error: message_id is required"
    max_chars = max(int(args.get("max_chars") or 8000), 200)
    try:
        msg = api_request(
            "GET",
            f"{GMAIL_API}/messages/{message_id}",
            params={"format": "full"},
            config=config,
        )
    except GoogleAuthError as exc:
        return f"Error: {exc}"
    result = message_summary(msg)
    body = extract_body(msg)
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated at {max_chars} characters]"
    result["body"] = body
    return json.dumps(result, indent=2, ensure_ascii=False)
