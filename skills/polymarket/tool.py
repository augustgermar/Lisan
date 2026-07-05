from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "polymarket_client.py"


def build_cli_args(args: dict[str, Any]) -> list[str] | str:
    action = str(args.get("action") or "").strip()
    query = str(args.get("query") or "").strip()
    slug = str(args.get("slug") or "").strip()
    token_id = str(args.get("token_id") or "").strip()

    if action == "search":
        return ["search", query] if query else "Error: query is required for search"
    if action == "trending":
        limit = str(min(max(int(args.get("limit") or 10), 1), 25))
        return ["trending", "--limit", limit]
    if action in ("market", "event"):
        return [action, slug] if slug else f"Error: slug is required for {action}"
    if action in ("price", "book"):
        return [action, token_id] if token_id else f"Error: token_id is required for {action}"
    return "Error: unknown action — use search, trending, market, event, price, or book"


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    cli_args = build_cli_args(args)
    if isinstance(cli_args, str):
        return cli_args
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *cli_args],
            capture_output=True,
            text=True,
            timeout=45.0,
        )
    except subprocess.TimeoutExpired:
        return "Error: Polymarket lookup timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return f"Error: Polymarket lookup failed: {detail or 'no output'}"
    return result.stdout.strip() or "No results."
