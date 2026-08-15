"""Core interface for scoped owner-source research.

This module is deliberately not the public-web adapter.  Ship 2 uses it for
Ring 1 sources (Gmail, Obsidian, and explicitly configured local roots).  A
provider may be backed by an installed skill, an API, or native local code;
enrichment depends on this contract, never on a particular skill being
installed.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


class SourceProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> list["SourceFinding"]: ...


@dataclass(frozen=True, slots=True)
class SourceFinding:
    source: str
    locator: str
    excerpt: str
    title: str = ""
    observed_at: str = ""


def search_owner_sources(
    query: str,
    *,
    providers: Iterable[SourceProvider] = (),
    max_results_per_source: int = 5,
) -> list[SourceFinding]:
    """Search supplied Ring 1 providers without making an LLM call.

    Missing or failing providers are represented by absence of findings.  The
    caller can log provider failures separately; one unavailable source must
    not silently prevent the remaining sources from being searched.
    """
    query = str(query or "").strip()
    if not query:
        return []
    provider_list = list(providers)
    findings: list[SourceFinding] = []
    for provider in provider_list:
        try:
            findings.extend(provider.search(query, limit=max_results_per_source))
        except Exception:
            continue
    return findings[: max(1, int(max_results_per_source)) * max(1, len(provider_list))]


class LocalRootProvider:
    """Read-only deterministic search over explicitly configured roots.

    This is intentionally bounded and opt-in.  It is a provider seam, not a
    permission to walk the user's home directory or whole disk.
    """

    name = "local_files"
    _MAX_FILE_BYTES = 5 * 1024 * 1024
    _DEFAULT_EXTENSIONS = {".md", ".txt", ".json", ".csv", ".pdf"}

    def __init__(self, roots: Iterable[Path], *, extensions: Iterable[str] | None = None) -> None:
        self.roots = [Path(root).expanduser().resolve() for root in roots]
        self.extensions = {str(ext).lower() for ext in (extensions or self._DEFAULT_EXTENSIONS)}

    def search(self, query: str, *, limit: int) -> list[SourceFinding]:
        terms = [t.lower() for t in re.findall(r"[\w'-]+", query) if len(t) > 1]
        if not terms:
            return []
        hits: list[SourceFinding] = []
        for root in self.roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in self.extensions:
                    continue
                try:
                    if path.stat().st_size > self._MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lowered = text.lower()
                matched = sum(1 for term in terms if term in lowered)
                if not matched:
                    continue
                excerpt = _excerpt(text, terms)
                hits.append(SourceFinding(self.name, str(path), excerpt, path.name))
        hits.sort(key=lambda item: (-sum(t in item.excerpt.lower() for t in terms), item.locator))
        return hits[: max(1, int(limit))]


class SkillSourceProvider:
    """Adapter that keeps an executable skill behind the core interface."""

    def __init__(self, name: str, handler: Callable[..., str]) -> None:
        self.name = name
        self._handler = handler

    def search(self, query: str, *, limit: int) -> list[SourceFinding]:
        if self.name == "gmail_search":
            raw = self._handler(query=query, max_results=limit)
            try:
                rows = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return []
            if not isinstance(rows, list):
                return []
            return [
                SourceFinding(
                    source=self.name,
                    locator=f"gmail://message/{row.get('id', '')}",
                    excerpt=str(row.get("snippet") or row.get("subject") or "")[:1200],
                    title=str(row.get("subject") or ""),
                    observed_at=str(row.get("date") or ""),
                )
                for row in rows
                if isinstance(row, dict) and (row.get("snippet") or row.get("subject"))
            ]
        if self.name == "obsidian_search":
            raw = self._handler(query=query, limit=limit)
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return []
            rows = payload.get("results") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                return []
            return [
                SourceFinding(
                    source=self.name,
                    locator=str(row.get("path") or ""),
                    excerpt=" ".join(str(item) for item in (row.get("snippets") or []))[:1200],
                    title=str(row.get("title") or ""),
                )
                for row in rows
                if isinstance(row, dict) and (row.get("path") or row.get("snippets"))
            ]
        return []


def installed_owner_providers(*, vault: Path, config: dict[str, Any]) -> list[SourceProvider]:
    """Return available Ring 1 providers; missing skills are a clean miss."""
    providers: list[SourceProvider] = []
    local = ((config.get("sources") or {}).get("local_files") or {})
    if local.get("enabled") and local.get("roots"):
        providers.append(LocalRootProvider(local["roots"], extensions=local.get("include_extensions")))
    try:
        from ..paths import skills_root
        from .skill_loader import load_skill_handlers

        handlers = load_skill_handlers(
            skills_root(), vault=vault, config=config, approval_fn=lambda *_args, **_kw: False,
        )
    except Exception:
        handlers = {}
    for name in ("gmail_search", "obsidian_search"):
        handler = handlers.get(name)
        if callable(handler):
            providers.append(SkillSourceProvider(name, handler))
    return providers


def _excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and any(term in line.lower() for term in terms):
            return line[:max_chars]
    return " ".join(text.split())[:max_chars]
