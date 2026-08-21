"""Core interfaces for scoped owner-source and published-world research.

Providers may be backed by an installed skill, an API, native local code, or
the standard-library web adapter below; enrichment depends on this contract,
never on a particular skill being installed.
"""
from __future__ import annotations

import os
import re
import json
import html
import base64
import ipaddress
import urllib.error
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
    document_text: str = ""


def search_owner_sources(
    query: str,
    *,
    providers: Iterable[SourceProvider] = (),
    max_results_per_source: int = 5,
    errors: list[str] | None = None,
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
        except Exception as exc:
            # One provider's outage must not stop the others, but it must not
            # look like "nothing matched" either. A caller that passes no
            # errors list is choosing the old silence explicitly.
            if errors is not None:
                errors.append(f"{getattr(provider, 'name', 'provider')}: {exc}")
            continue
    return findings[: max(1, int(max_results_per_source)) * max(1, len(provider_list))]


def search_published_sources(
    query: str,
    *,
    providers: Iterable[SourceProvider] = (),
    max_results_per_source: int = 5,
    errors: list[str] | None = None,
) -> list[SourceFinding]:
    """Search explicitly enabled Ring 2 providers with bounded results."""
    return search_owner_sources(
        query, providers=providers, max_results_per_source=max_results_per_source,
        errors=errors,
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


class _PageParser(HTMLParser):
    """Extract visible text and links from one HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._skip_depth = 0
        self._link_href = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._link_href = html.unescape(str(attrs_dict.get("href") or ""))
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        self.text.append(clean)
        if self._in_title:
            self.title.append(clean)
        if self._link_href:
            self._link_text.append(clean)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_href:
            self.links.append((self._link_href, " ".join(self._link_text)))
            self._link_href, self._link_text = "", []


def _public_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password or (parsed.port not in (None, 80, 443)):
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified)
    except ValueError:
        return False


def _normalize_search_result_url(value: str) -> str:
    """Resolve Bing click tracking before provenance is recorded.

    A search-engine redirect is not a source origin. If it cannot be decoded,
    return an empty value so the result is omitted rather than attributed to
    the search engine.
    """
    value = html.unescape(str(value or "").strip())
    try:
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host.endswith("bing.com") and parsed.path.startswith("/ck/"):
            encoded = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
            if encoded.startswith("a1"):
                raw = encoded[2:]
                raw += "=" * (-len(raw) % 4)
                decoded = base64.urlsafe_b64decode(raw).decode("utf-8", errors="strict")
                if _public_http_url(decoded):
                    return decoded
            return ""
    except (ValueError, UnicodeError, base64.binascii.Error):
        return ""
    return value


class WebSearchProvider:
    """Bounded search-and-crawl adapter for the published world.

    Search results seed a breadth-limited crawl. Pages are followed at most
    ``max_depth`` links from a result, with hard page/byte/link caps. The
    opener is injectable so the contract is deterministic in tests and a
    deployment can replace the default endpoint with an approved search API.
    """

    name = "web_search"

    def __init__(
        self,
        *,
        endpoint: str = "https://www.bing.com/search",
        timeout: float = 20.0,
        max_depth: int = 3,
        max_pages: int = 12,
        max_links_per_page: int = 8,
        max_page_bytes: int = 1_000_000,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.max_depth = max(0, int(max_depth))
        self.max_pages = max(1, int(max_pages))
        self.max_links_per_page = max(1, int(max_links_per_page))
        self.max_page_bytes = max(1_000, int(max_page_bytes))
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
        rows = []
        for row in parser.rows:
            normalized_url = _normalize_search_result_url(row.get("url", ""))
            if normalized_url and _public_http_url(normalized_url):
                rows.append({**row, "url": normalized_url})
        findings: list[SourceFinding] = []
        queued: list[tuple[str, int, str, str]] = [
            (row["url"], 0, row["title"], row.get("excerpt") or row["title"])
            for row in rows[: max(1, int(limit))]
        ]
        visited: set[str] = set()
        pages = 0
        terms = [term.lower() for term in re.findall(r"[\w'-]+", query) if len(term) > 1]
        while queued and pages < self.max_pages and len(findings) < max(1, int(limit)):
            url, depth, seed_title, seed_excerpt = queued.pop(0)
            normalized = urllib.parse.urldefrag(url)[0]
            if normalized in visited or not _public_http_url(normalized):
                continue
            visited.add(normalized)
            page = self._fetch_page(normalized)
            pages += 1
            if page is None:
                if depth == 0 and seed_excerpt:
                    findings.append(self._finding(normalized, seed_title, seed_excerpt, retrieved, 0.35))
                continue
            title, text, links = page
            lowered = f"{title} {text}".lower()
            score = sum(lowered.count(term) for term in terms)
            if score:
                excerpt = _page_excerpt(text, terms) or seed_excerpt or title
                findings.append(self._finding(normalized, title or seed_title, excerpt, retrieved, min(0.75, 0.45 + score / 20)))
            if depth >= self.max_depth:
                continue
            base_host = urllib.parse.urlparse(normalized).netloc
            ranked_links = sorted(
                links,
                key=lambda item: (
                    0 if urllib.parse.urlparse(urllib.parse.urljoin(normalized, item[0])).netloc == base_host else 1,
                    0 if any(term in (item[0] + " " + item[1]).lower() for term in terms) else 1,
                ),
            )
            for href, anchor in ranked_links[: self.max_links_per_page]:
                child = urllib.parse.urldefrag(urllib.parse.urljoin(normalized, href))[0]
                if _public_http_url(child) and child not in visited:
                    queued.append((child, depth + 1, anchor or title, anchor or title))
        if not findings:
            for row in rows[: max(1, int(limit))]:
                findings.append(self._finding(
                    row["url"], row["title"], row.get("excerpt") or row["title"], retrieved, 0.3,
                ))
        return findings[: max(1, int(limit))]

    def _fetch_page(self, url: str) -> tuple[str, str, list[tuple[str, str]]] | None:
        request = urllib.request.Request(url, headers={"User-Agent": "Lisan/1.0 scoped-research"}, method="GET")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
                if content_type and "html" not in content_type and "xhtml" not in content_type:
                    return None
                try:
                    body = response.read(self.max_page_bytes + 1)
                except TypeError:
                    body = response.read()
                if len(body) > self.max_page_bytes:
                    body = body[: self.max_page_bytes]
                parser = _PageParser()
                parser.feed(body.decode("utf-8", errors="replace"))
                return " ".join(parser.title), " ".join(parser.text), parser.links
        except Exception:
            return None

    def _finding(self, url: str, title: str, excerpt: str, retrieved: str, confidence: float) -> SourceFinding:
        return SourceFinding(
            source=self.name, locator=url, excerpt=excerpt[:1200], title=title[:300],
            observed_at=retrieved, publisher=urllib.parse.urlparse(url).netloc,
            retrieved_at=retrieved, confidence=confidence, unverifiable=True,
        )


def _page_excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    words = text.split()
    for index, word in enumerate(words):
        if any(term in word.lower() for term in terms):
            start = max(0, index - 35)
            return " ".join(words[start : start + 90])[:max_chars]
    return " ".join(words)[:max_chars]


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


class SearchProviderError(RuntimeError):
    """A search backend could not answer — as distinct from finding nothing.

    The 2026-08-21 audit found the opposite failure: a backend that returned
    ten well-formed results belonging to somebody else's query, with no way
    for a caller to tell them from real ones. Every backend added here owes
    the caller that distinction.
    """


def search_credentials_path(provider: str) -> Path:
    """Where a search backend's key lives: ``<credentials_root>/<provider>.json``."""
    from ..paths import credentials_root

    return credentials_root() / f"{str(provider or 'search').strip().lower()}.json"


def _key_from_credentials_file(provider: str) -> str:
    """Read the API key from the credentials store, or "" if absent."""
    try:
        path = search_credentials_path(provider)
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str((data or {}).get("api_key") or "").strip() if isinstance(data, dict) else ""


def resolve_search_api_key(config: dict[str, Any] | None = None, *, provider: str = "tavily") -> str:
    """Environment first, then the credentials store.

    Detached services (jobs, telegram, adjutant) inherit no shell
    environment, so a key that lives only in an env var works when the owner
    tests it by hand and fails silently everywhere that matters. The
    credentials file is visible to all of them.
    """
    web = ((config or {}).get("sources") or {}).get("web") or {}
    env_name = str(web.get("api_key_env") or f"{provider.upper()}_API_KEY")
    return str(os.getenv(env_name) or "").strip() or _key_from_credentials_file(provider)


class BraveSearchProvider:
    """Web discovery through the Brave Search API.

    Replaces HTML scraping of a consumer search engine, which by 2026-08-21
    returned results unrelated to the query — reproducibly, and differently
    on each call.
    """

    name = "web_search"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.search.brave.com/res/v1/web/search",
        timeout: float = 20.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen

    def search(self, query: str, *, limit: int) -> list[SourceFinding]:
        query = str(query or "").strip()
        if not query:
            return []
        if not self.api_key:
            raise SearchProviderError(
                "no Brave Search API key: set BRAVE_API_KEY or write "
                f"{search_credentials_path('brave')} as {{\"api_key\": \"...\"}}"
            )
        count = max(1, min(int(limit), 20))
        url = f"{self.endpoint}?{urllib.parse.urlencode({'q': query, 'count': count})}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Lisan/1.0 scoped-research",
                "X-Subscription-Token": self.api_key,
            },
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = {401: "key rejected", 403: "key not authorized for this endpoint", 429: "rate limit or quota exhausted"}.get(exc.code, "")
            raise SearchProviderError(f"Brave Search returned HTTP {exc.code}{': ' + detail if detail else ''}") from exc
        except Exception as exc:
            raise SearchProviderError(f"Brave Search unreachable: {exc}") from exc
        results = ((payload or {}).get("web") or {}).get("results") or []
        retrieved = datetime.now(timezone.utc).isoformat()
        findings: list[SourceFinding] = []
        for item in results[:count]:
            url_value = str((item or {}).get("url") or "")
            if not _public_http_url(url_value):
                continue
            title = str(item.get("title") or "")
            excerpt = re.sub(r"<[^>]+>", "", str(item.get("description") or "")) or title
            findings.append(SourceFinding(
                source=self.name, locator=url_value, excerpt=excerpt[:1200], title=title[:300],
                observed_at=retrieved, publisher=urllib.parse.urlparse(url_value).netloc,
                published_at=str(item.get("age") or ""), retrieved_at=retrieved,
                confidence=0.5, unverifiable=True,
            ))
        return findings


class TavilySearchProvider:
    """Web discovery through the Tavily search API.

    Chosen as the default on 2026-08-21: Brave retired its free tier in
    February 2026, and Google's Custom Search JSON API is closed to new
    customers and shuts down 2027-01-01. Tavily grants 1,000 credits a
    month without a card, and returns extracted page text rather than only
    a snippet, which is what the librarian ingests.
    """

    name = "web_search"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.tavily.com/search",
        timeout: float = 20.0,
        search_depth: str = "basic",
        include_raw_content: bool = False,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.endpoint = endpoint
        self.timeout = float(timeout)
        # "advanced" costs two credits per call; the owner's monthly grant is
        # small enough that the default stays at one.
        self.search_depth = str(search_depth or "basic")
        self.include_raw_content = bool(include_raw_content)
        self.opener = opener or urllib.request.urlopen

    def search(self, query: str, *, limit: int) -> list[SourceFinding]:
        query = str(query or "").strip()
        if not query:
            return []
        if not self.api_key:
            raise SearchProviderError(
                "no Tavily API key: set TAVILY_API_KEY or write "
                f"{search_credentials_path('tavily')} as {{\"api_key\": \"...\"}}"
            )
        body = json.dumps({
            "query": query,
            "max_results": max(1, min(int(limit), 20)),
            "search_depth": self.search_depth,
            "include_raw_content": self.include_raw_content,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Lisan/1.0 scoped-research",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = {401: "key rejected", 403: "key not authorized", 429: "rate limit or monthly credits exhausted", 432: "monthly credits exhausted"}.get(exc.code, "")
            raise SearchProviderError(f"Tavily returned HTTP {exc.code}{': ' + detail if detail else ''}") from exc
        except Exception as exc:
            raise SearchProviderError(f"Tavily unreachable: {exc}") from exc
        retrieved = datetime.now(timezone.utc).isoformat()
        findings: list[SourceFinding] = []
        for item in ((payload or {}).get("results") or [])[: max(1, int(limit))]:
            url_value = str((item or {}).get("url") or "")
            if not _public_http_url(url_value):
                continue
            title = str(item.get("title") or "")
            content = str(item.get("content") or "") or title
            score = item.get("score")
            findings.append(SourceFinding(
                source=self.name, locator=url_value, excerpt=content[:1200], title=title[:300],
                observed_at=retrieved, publisher=urllib.parse.urlparse(url_value).netloc,
                retrieved_at=retrieved,
                # Relevance is not authority: the owner still approves every
                # origin, so this never exceeds the unverified band.
                confidence=min(0.5, float(score)) if isinstance(score, (int, float)) else 0.4,
                unverifiable=True,
                document_text=str(item.get("raw_content") or ""),
            ))
        return findings


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
    provider = str(web.get("provider") or "tavily").strip().lower()
    if provider == "tavily":
        return [TavilySearchProvider(
            api_key=resolve_search_api_key(config, provider="tavily"),
            endpoint=str(web.get("search_endpoint") or "https://api.tavily.com/search"),
            timeout=float(web.get("timeout_seconds") or 20),
            search_depth=str(web.get("search_depth") or "basic"),
            include_raw_content=bool(web.get("include_raw_content") or False),
        )]
    if provider == "brave":
        return [BraveSearchProvider(
            api_key=resolve_search_api_key(config, provider="brave"),
            endpoint=str(web.get("search_endpoint") or "https://api.search.brave.com/res/v1/web/search"),
            timeout=float(web.get("timeout_seconds") or 20),
        )]
    if provider in {"html_scrape", "bing_scrape"}:
        # Retained for offline fixtures and for an owner who knowingly wants
        # it. Not a default: on 2026-08-21 this path returned ten well-formed
        # results per query, none of them related to the query.
        return [WebSearchProvider(
            endpoint=str(web.get("endpoint") or "https://www.bing.com/search"),
            timeout=float(web.get("timeout_seconds") or 20),
            max_depth=int(web.get("max_depth") or 3),
            max_pages=int(web.get("max_pages") or 12),
            max_links_per_page=int(web.get("max_links_per_page") or 8),
            max_page_bytes=int(web.get("max_page_bytes") or 1_000_000),
        )]
    raise SearchProviderError(f"unknown sources.web.provider: {provider!r}")


def _excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and any(term in line.lower() for term in terms):
            return line[:max_chars]
    return " ".join(text.split())[:max_chars]
