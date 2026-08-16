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
        return _Response(
            '<a class="result__a" href="https://example.test/a">Example result</a>'
            '<a class="result__a" href="https://example.test/b">Second result</a>'
        )

    provider = WebSearchProvider(opener=opener, timeout=3)
    findings = provider.search("a named deficit", limit=1)
    assert len(findings) == 1
    assert findings[0].source == "web_search"
    assert findings[0].locator == "https://example.test/a"
    assert findings[0].publisher == "example.test"
    assert findings[0].unverifiable is True
    assert "a+named+deficit" in seen["url"]
    assert seen["timeout"] == 3.0


def test_published_provider_is_explicitly_enabled():
    assert installed_published_providers(config={}) == []
    providers = installed_published_providers(config={"sources": {"web": {"enabled": True}}})
    assert len(providers) == 1
    assert providers[0].name == "web_search"
