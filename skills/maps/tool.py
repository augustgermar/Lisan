from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "maps_client.py"


def build_cli_args(args: dict[str, Any]) -> list[str] | str:
    """Map tool args to maps_client.py CLI args. Returns an error string on
    bad input so the model gets a correctable message instead of a stack."""
    action = str(args.get("action") or "").strip()
    query = str(args.get("query") or "").strip()
    lat, lon = args.get("lat"), args.get("lon")
    limit = str(min(max(int(args.get("limit") or 10), 1), 50))

    if action == "search":
        if not query:
            return "Error: query is required for search"
        return ["search", query]
    if action == "area":
        if not query:
            return "Error: query is required for area"
        return ["area", query]
    if action in ("reverse", "timezone"):
        if lat is None or lon is None:
            return f"Error: lat and lon are required for {action}"
        return [action, str(lat), str(lon)]
    if action == "nearby":
        category = str(args.get("category") or "").strip()
        if not category:
            return "Error: category is required for nearby"
        radius = str(min(max(int(args.get("radius") or 500), 50), 20000))
        near = str(args.get("near") or "").strip()
        if near:
            return ["nearby", "--near", near, "--category", category,
                    "--radius", radius, "--limit", limit]
        if lat is None or lon is None:
            return "Error: pass 'near' (a place name) or lat+lon for nearby"
        return ["nearby", str(lat), str(lon), category, "--radius", radius, "--limit", limit]
    if action in ("distance", "directions"):
        origin = str(args.get("origin") or "").strip()
        destination = str(args.get("destination") or "").strip()
        if not origin or not destination:
            return f"Error: origin and destination are required for {action}"
        mode = str(args.get("mode") or "driving")
        if mode not in ("driving", "walking", "cycling"):
            mode = "driving"
        return [action, origin, "--to", destination, "--mode", mode]
    return (
        "Error: unknown action — use search, reverse, nearby, distance, "
        "directions, timezone, or area"
    )


def run(args: dict[str, Any], vault: Path, config: dict[str, Any]) -> str:
    cli_args = build_cli_args(args)
    if isinstance(cli_args, str):
        return cli_args
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *cli_args],
            capture_output=True,
            text=True,
            timeout=90.0,
        )
    except subprocess.TimeoutExpired:
        return "Error: maps lookup timed out (OpenStreetMap services may be slow)"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        return f"Error: maps lookup failed: {detail or 'no output'}"
    return result.stdout.strip() or "No results."
