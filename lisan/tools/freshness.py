"""Freshness metadata rendered at context boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Freshness:
    observed_at: str
    valid_until: str | None
    age_days: int
    status: str


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def metadata(frontmatter: dict[str, Any], *, now: datetime | None = None, stable: bool = False, default_ttl_days: int | None = None) -> Freshness:
    now = now or datetime.now(timezone.utc)
    observed = _parse(frontmatter.get("observed_at") or frontmatter.get("updated") or frontmatter.get("created")) or now
    valid = _parse(frontmatter.get("valid_until") or frontmatter.get("review_after"))
    ttl = frontmatter.get("ttl_days")
    if valid is None and ttl not in (None, ""):
        try:
            valid = observed + timedelta(days=int(ttl))
        except (TypeError, ValueError):
            pass
    if valid is None and default_ttl_days is not None and not stable:
        valid = observed + timedelta(days=default_ttl_days)
    age_days = max(0, (now.date() - observed.date()).days)
    status = "stable" if stable else ("historical" if valid is not None and now > valid else "current")
    return Freshness(observed.isoformat(), valid.isoformat() if valid else None, age_days, status)


def line(frontmatter: dict[str, Any], *, stable: bool = False, default_ttl_days: int | None = None) -> str:
    item = metadata(frontmatter, stable=stable, default_ttl_days=default_ttl_days)
    valid = item.valid_until or "never"
    return f"- freshness: observed_at={item.observed_at} | valid_until={valid} | age={item.age_days}d | status={item.status}"


def file_line(path: Path, *, stable: bool = False, default_ttl_days: int | None = None) -> str:
    observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return line({"observed_at": observed}, stable=stable, default_ttl_days=default_ttl_days)
