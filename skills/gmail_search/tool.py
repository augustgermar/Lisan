from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_google_common"))

from lisan_google import GMAIL_API, GoogleAuthError, api_request, message_summary  # noqa: E402


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: query is required"
    max_results = min(max(int(args.get("max_results") or 10), 1), 25)
    try:
        listing = api_request(
            "GET",
            f"{GMAIL_API}/messages",
            params={"q": query, "maxResults": max_results},
            config=config,
        )
        results = []
        for meta in listing.get("messages", []):
            msg = api_request(
                "GET",
                f"{GMAIL_API}/messages/{meta['id']}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                },
                config=config,
            )
            results.append(message_summary(msg))
    except GoogleAuthError as exc:
        return f"Error: {exc}"
    if not results:
        return f"No messages matched {query!r}."
    return json.dumps(results, indent=2, ensure_ascii=False)
