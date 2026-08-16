"""Core interfaces for scoped owner-source and published-world research.

Providers may be backed by an installed skill, an API, native local code, or
the standard-library web adapter below; enrichment depends on this contract,
never on a particular skill being installed.
"""
from __future__ import annotations

import re
import json
import html
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone
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
    publisher: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    confidence: float | None = None
    disagreement: str = ""
    unverifiable: bool = False


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


def search_published_sources(
    query: str,
    *,
    providers: Iterable[SourceProvider] = (),
    max_results_per_source: int = 5,
) -> list[SourceFinding]:
    """Search explicitly enabled Ring 2 providers with bounded results."""
    return search_owner_sources(
        query, providers=providers, max_results_per_source=max_results_per_source,
    )


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


class _SearchResultParser(HTMLParser):
    """Small parser for search-result pages; never follows result links."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []
        self._in_result = False
        self._bing_row: dict[str, str] | None = None
        self._bing_title = False
        self._bing_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = str(attrs_dict.get("class") or "")
        if tag == "li" and "b_algo" in classes:
            self._bing_row = {"title": "", "url": "", "snippet": ""}
        if self._bing_row is not None and tag == "h2":
            self._bing_title = True
        if self._bing_row is not None and tag == "p":
            self._bing_snippet = True
        if self._bing_row is not None and tag == "a" and self._bing_title:
            self._bing_row["url"] = html.unescape(str(attrs_dict.get("href") or ""))
        if tag == "a" and "result__a" in classes:
            self._href = html.unescape(str(attrs_dict.get("href") or ""))
            self._text = []
            self._in_result = True

    def handle_data(self, data: str) -> None:
        if self._in_result:
            self._text.append(data)
        if self._bing_row is not None:
            if self._bing_title:
                self._bing_row["title"] += data
            elif self._bing_snippet:
                self._bing_row["snippet"] += data

    def handle_endtag(self, tag: str) -> None:
        if self._bing_row is not None:
            if tag == "h2":
                self._bing_title = False
            elif tag == "p":
                self._bing_snippet = False
            elif tag == "li":
                row = {k: " ".join(v.split()) for k, v in self._bing_row.items()}
                if row["url"] and row["title"]:
                    self.rows.append({"title": row["title"][:300], "url": row["url"], "excerpt": row["snippet"][:1200]})
                self._bing_row = None
        if tag == "a" and self._in_result:
            title = " ".join("".join(self._text).split())
            if self._href and title:
                self.rows.append({"title": title[:300], "url": self._href})
            self._href, self._text, self._in_result = "", [], False


class WebSearchProvider:
    """Bounded search-result adapter for the published world.

    It retrieves result metadata/snippets only and never crawls result pages.
    The opener is injectable so the contract is deterministic in tests and a
    deployment can replace the default endpoint with an approved search API.
    """

    name = "web_search"

    def __init__(
        self,
        *,
        endpoint: str = "https://www.bing.com/search",
        timeout: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen

    def search(self, query: str, *, limit: int) -> list[SourceFinding]:
        query = str(query or "").strip()
        if not query:
            return []
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query})}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "Lisan/1.0 scoped-research"}, method="GET"
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        parser = _SearchResultParser()
        parser.feed(body)
        retrieved = datetime.now(timezone.utc).isoformat()
        findings: list[SourceFinding] = []
        for row in parser.rows[: max(1, int(limit))]:
            findings.append(SourceFinding(
                source=self.name,
                locator=row["url"],
                excerpt=row.get("excerpt") or row["title"],
                title=row["title"],
                observed_at=retrieved,
                publisher=urllib.parse.urlparse(row["url"]).netloc,
                retrieved_at=retrieved,
                confidence=0.4,
                unverifiable=True,
            ))
        return findings


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


def installed_published_providers(*, config: dict[str, Any]) -> list[SourceProvider]:
    """Return explicitly enabled Ring 2 providers; disabled by default."""
    web = (config.get("sources") or {}).get("web") or {}
    if not web.get("enabled"):
        return []
    return [WebSearchProvider(
        endpoint=str(web.get("endpoint") or "https://www.bing.com/search"),
        timeout=float(web.get("timeout_seconds") or 20),
    )]


def _excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and any(term in line.lower() for term in terms):
            return line[:max_chars]
    return " ".join(text.split())[:max_chars]
