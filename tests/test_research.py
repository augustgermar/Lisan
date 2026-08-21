from __future__ import annotations

from lisan.tools.research import BraveSearchProvider, BrowserSearchProvider, SearchProviderError, TavilySearchProvider, WebSearchProvider, installed_published_providers, search_published_sources
from lisan.tools.research import _normalize_search_result_url


class _Response:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_web_provider_returns_structured_bounded_findings():
    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        if "bing.com/search" in request.full_url:
            seen["search_url"] = request.full_url
            body = (
                '<a class="result__a" href="https://example.test/a">Example result</a>'
                '<a class="result__a" href="https://example.test/b">Second result</a>'
            )
        else:
            body = "<html><title>Example result</title><p>Example result</p></html>"
        return _Response(
            body
        )

    provider = WebSearchProvider(opener=opener, timeout=3)
    findings = provider.search("a named deficit", limit=1)
    assert len(findings) == 1
    assert findings[0].source == "web_search"
    assert findings[0].locator == "https://example.test/a"
    assert findings[0].publisher == "example.test"
    assert findings[0].unverifiable is True
    assert "a+named+deficit" in seen["search_url"]
    assert seen["timeout"] == 3.0


def test_web_provider_follows_links_up_to_configured_depth():
    requested = []

    def opener(request, *, timeout):
        requested.append(request.full_url)
        if "bing.com/search" in request.full_url:
            body = '<a class="result__a" href="https://example.test/root">Root</a>'
        elif request.full_url.endswith("/root"):
            body = '<a href="/one">one</a>'
        elif request.full_url.endswith("/one"):
            body = '<a href="/two">two</a>'
        elif request.full_url.endswith("/two"):
            body = '<p>The target phrase is here.</p><a href="/three">three</a>'
        else:
            body = '<p>The target phrase is deeper still.</p>'
        return _Response(body)

    findings = WebSearchProvider(opener=opener, max_depth=3, max_pages=10).search(
        "target phrase", limit=2,
    )
    assert len(findings) == 2
    assert findings[0].locator.endswith("/two")
    assert findings[1].locator.endswith("/three")
    assert any(url.endswith("/three") for url in requested)


def test_web_provider_rejects_private_and_non_http_links():
    def opener(request, *, timeout):
        return _Response(
            '<a class="result__a" href="http://127.0.0.1/private">private</a>'
            '<a class="result__a" href="file:///tmp/secret">file</a>'
        )

    assert WebSearchProvider(opener=opener).search("private", limit=2) == []


def test_published_provider_is_explicitly_enabled():
    assert installed_published_providers(config={}) == []
    providers = installed_published_providers(config={"sources": {"web": {"enabled": True}}})
    assert len(providers) == 1
    assert providers[0].name == "web_search"


def test_bing_redirect_is_resolved_before_provenance():
    import base64
    import urllib.parse

    target = "https://www.rfc-editor.org/rfc/rfc9110.html"
    encoded = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    redirect = "https://www.bing.com/ck/a?" + urllib.parse.urlencode({"u": encoded})
    assert _normalize_search_result_url(redirect) == target


def test_backend_is_selected_by_config(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    providers = installed_published_providers(
        config={"sources": {"web": {"enabled": True, "provider": "tavily"}}}
    )
    assert [type(p).__name__ for p in providers] == ["TavilySearchProvider"]
    brave = installed_published_providers(
        config={"sources": {"web": {"enabled": True, "provider": "brave", "api_key_env": "BRAVE_API_KEY"}}}
    )
    assert [type(p).__name__ for p in brave] == ["BraveSearchProvider"]
    # The HTML scraper remains reachable, but only by explicit choice.
    scraped = installed_published_providers(
        config={"sources": {"web": {"enabled": True, "provider": "html_scrape"}}}
    )
    assert [type(p).__name__ for p in scraped] == ["WebSearchProvider"]


def test_tavily_provider_parses_results_and_authenticates(monkeypatch):
    import json as _json

    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = _json.loads(request.data.decode())
        return _Response(_json.dumps({"results": [
            {"url": "https://www.youtube.com/@psychacks", "title": "Orion Taraban - YouTube",
             "content": "Psyhacks channel", "score": 0.93, "raw_content": "full page text"},
            {"url": "http://10.0.0.1/admin", "title": "private", "content": "no"},
        ]}))

    provider = TavilySearchProvider(api_key="tvly-test", opener=opener)
    findings = provider.search("Psyhacks Orion Taraban", limit=5)
    assert seen["auth"] == "Bearer tvly-test"
    assert seen["body"]["query"] == "Psyhacks Orion Taraban"
    assert seen["body"]["search_depth"] == "basic"  # one credit, not two
    assert [f.locator for f in findings] == ["https://www.youtube.com/@psychacks"]
    assert findings[0].document_text == "full page text"
    # A high relevance score is not a claim of authority.
    assert findings[0].confidence <= 0.5


def test_tavily_provider_without_a_key_names_the_fix():
    try:
        TavilySearchProvider(api_key="").search("anything", limit=3)
    except SearchProviderError as exc:
        assert "TAVILY_API_KEY" in str(exc) and "tavily.json" in str(exc)
    else:
        raise AssertionError("a missing key must raise, never return []")


def test_brave_provider_parses_results_and_sends_the_key(monkeypatch):
    import json as _json

    seen = {}

    def opener(request, *, timeout):
        seen["url"] = request.full_url
        seen["key"] = request.get_header("X-subscription-token")
        return _Response(_json.dumps({"web": {"results": [
            {"url": "https://www.youtube.com/@psychacks", "title": "Orion Taraban - YouTube",
             "description": "Psy<strong>hacks</strong> channel"},
            {"url": "http://127.0.0.1/admin", "title": "local", "description": "private"},
        ]}}))

    provider = BraveSearchProvider(api_key="test-key", opener=opener)
    findings = provider.search("Psyhacks Orion Taraban", limit=5)
    assert "q=Psyhacks+Orion+Taraban" in seen["url"]
    assert seen["key"] == "test-key"
    # Private addresses are refused, and markup never reaches the excerpt.
    assert [f.locator for f in findings] == ["https://www.youtube.com/@psychacks"]
    assert findings[0].excerpt == "Psyhacks channel"
    assert findings[0].publisher == "www.youtube.com"


def test_search_backend_failure_is_reported_not_swallowed():
    """A backend that cannot answer must not look like a query with no
    matches. The 2026-08-21 audit found silent failure indistinguishable
    from an empty result set."""

    class Broken:
        name = "web_search"

        def search(self, query, *, limit):
            raise SearchProviderError("key rejected")

    errors: list[str] = []
    findings = search_published_sources("anything", providers=[Broken()], errors=errors)
    assert findings == []
    assert errors == ["web_search: key rejected"]
    # Callers that pass no list keep the old behaviour and get no exception.
    assert search_published_sources("anything", providers=[Broken()]) == []


def test_brave_provider_without_a_key_names_the_fix():
    provider = BraveSearchProvider(api_key="")
    try:
        provider.search("anything", limit=3)
    except SearchProviderError as exc:
        assert "BRAVE_API_KEY" in str(exc) and "brave.json" in str(exc)
    else:
        raise AssertionError("a missing key must raise, never return []")


def test_browser_backend_is_the_default_and_needs_no_account():
    providers = installed_published_providers(config={"sources": {"web": {"enabled": True}}})
    assert [type(p).__name__ for p in providers] == ["BrowserSearchProvider"]


def test_browser_provider_tries_each_engine_and_reports_total_failure():
    calls = []

    def searcher(query, *, limit, engine, settle_seconds):
        calls.append(engine)
        if engine == "google":
            return {"ok": False, "error": "consent wall"}
        return {"ok": True, "engine": engine, "results": [
            {"url": "https://www.youtube.com/@psychacks", "title": "Orion Taraban - PsycHacks",
             "snippet": "Psychology channel"},
            {"url": "https://192.168.1.5/admin", "title": "router", "snippet": "private"},
        ]}

    provider = BrowserSearchProvider(engines=("google", "duckduckgo"), searcher=searcher)
    findings = provider.search("Psyhacks", limit=5)
    # A consent wall on one engine is not an answer about the web.
    assert calls == ["google", "duckduckgo"]
    assert [f.locator for f in findings] == ["https://www.youtube.com/@psychacks"]

    def always_broken(query, *, limit, engine, settle_seconds):
        return {"ok": False, "error": "browser could not be started"}

    try:
        BrowserSearchProvider(engines=("google",), searcher=always_broken).search("x", limit=3)
    except SearchProviderError as exc:
        assert "browser could not be started" in str(exc)
    else:
        raise AssertionError("every engine failing must raise, never return []")
