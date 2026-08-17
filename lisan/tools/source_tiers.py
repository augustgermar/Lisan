"""Source-tier rules shared by librarian intake, ingestion, and retrieval."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

SOURCE_TIERS = ("primary", "official-secondary", "community", "owner-authored", "unverified")
_ORDER = {name: len(SOURCE_TIERS) - index for index, name in enumerate(SOURCE_TIERS)}


def normalize_source_tier(value: str | None) -> str:
    tier = str(value or "unverified").strip().lower()
    return tier if tier in _ORDER else "unverified"


def tier_rank(value: str | None) -> int:
    return _ORDER[normalize_source_tier(value)]


def tier_confidence(value: str | None) -> tuple[str, str]:
    tier = normalize_source_tier(value)
    return {
        "primary": ("high", "Source tier: primary (owner-approved authoritative origin)"),
        "official-secondary": ("high", "Source tier: official-secondary (official but not the authoritative origin)"),
        "community": ("medium", "Source tier: community (useful corroboration, not authoritative)"),
        "owner-authored": ("medium", "Source tier: owner-authored (owner material; verify against primary sources)"),
        "unverified": ("low", "Source tier: unverified (not yet accepted as authoritative)"),
    }[tier]


def origin_for_url(url: str | None) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    return (parsed.hostname or value).lower().rstrip(".")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def origin_matches(origin: str, url: str) -> bool:
    wanted = str(origin or "").lower().strip().rstrip(".")
    actual = origin_for_url(url)
    return bool(wanted and (actual == wanted or actual.endswith("." + wanted)))
