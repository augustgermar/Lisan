from __future__ import annotations

from lisan.tools.research import WebSearchProvider, installed_published_providers


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
