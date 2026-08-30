"""Gates on agent -> provider routing.

The defect these pin (2026-08-16): ``self_repair_author`` was added to the
codebase without a routing entry, and ``select_provider`` silently answered
with a hardcoded ``"local"``. Every self-repair proposal went to whatever
owned port 8080 — a different project's model server — and died with
"Remote end closed connection without response", which names neither the
agent nor the config as the cause. Two terminal job failures, one
investigation loop, and no signal anywhere that a routing entry was missing.

The routing-coverage test is the structural half: a new agent that calls an
LLM cannot ship without an entry, because the suite reads the source.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from lisan.config import DEFAULT_CONFIG
from lisan.providers.codex import _is_schema_echo
from lisan.providers.config import (
    ProviderRoutingError,
    resolve_route,
    select_provider,
)

_LISAN = Path(__file__).resolve().parents[1] / "lisan"
_AGENT_LITERAL = re.compile(r'agent="([a-zA-Z_][a-zA-Z_0-9]*)"')

_ROUTED = {
    "low": "codex",
    "medium": "codex",
    "high": "codex",
}


def _agent_names_in_source() -> set[str]:
    names: set[str] = set()
    for path in sorted(_LISAN.rglob("*.py")):
        names.update(_AGENT_LITERAL.findall(path.read_text(encoding="utf-8")))
    return names


class RoutingCoverageTests(unittest.TestCase):
    def test_every_agent_used_in_source_has_a_routing_entry(self) -> None:
        """The gate. An agent name appearing in an LLM call must be routable
        by name — not by falling through to the default, which exists for
        one-off CLI calls, not for shipped agents."""
        routing = DEFAULT_CONFIG["routing"]
        used = _agent_names_in_source()
        self.assertIn("self_repair_author", used, "sentinel: the regression agent must still be scanned")
        missing = sorted(name for name in used if name not in routing)
        self.assertEqual(
            missing,
            [],
            f"agents used in lisan/ with no DEFAULT_CONFIG routing entry: {missing}",
        )

    def test_default_config_ships_a_default_route(self) -> None:
        self.assertIn("default", DEFAULT_CONFIG["routing"])

    def test_self_repair_author_routes_to_codex(self) -> None:
        """The exact 2026-08-16 misroute: this agent must not resolve to the
        local endpoint."""
        selection = select_provider(DEFAULT_CONFIG, agent="self_repair_author", significance="high")
        self.assertEqual(selection.provider, "codex")

    def test_codex_schema_echo_is_detected(self) -> None:
        self.assertTrue(_is_schema_echo({"$schema": "https://json-schema.org", "type": "object", "properties": {}}))
        self.assertFalse(_is_schema_echo({"narrative": "actual answer"}))


class RouteResolutionTests(unittest.TestCase):
    def test_named_entry_wins(self) -> None:
        routing = {"default": _ROUTED, "writer": {"high": "openai"}}
        self.assertEqual(resolve_route(routing, "writer", "high"), "openai")

    def test_unrouted_agent_falls_back_to_default_not_local(self) -> None:
        routing = {"default": _ROUTED}
        self.assertEqual(resolve_route(routing, "brand_new_agent", "high"), "codex")

    def test_dotted_agent_inherits_parent_route(self) -> None:
        """`elicitor.prose_recovery` is the elicitor doing elicitor work; it
        must not need its own entry to avoid the fallback."""
        routing = {"default": _ROUTED, "elicitor": {"medium": "openai"}}
        self.assertEqual(resolve_route(routing, "elicitor.prose_recovery", "medium"), "openai")

    def test_deeply_dotted_agent_walks_up_to_the_nearest_entry(self) -> None:
        routing = {"default": _ROUTED, "elicitor": {"medium": "openai"}}
        self.assertEqual(resolve_route(routing, "elicitor.a.b", "medium"), "openai")

    def test_missing_significance_falls_through_to_default(self) -> None:
        routing = {"default": _ROUTED, "writer": {"low": "openai"}}
        self.assertEqual(resolve_route(routing, "writer", "high"), "codex")

    def test_no_entry_and_no_default_raises_naming_the_agent(self) -> None:
        """Silence is what caused the incident. With nothing to route to, the
        call fails loudly instead of guessing a provider."""
        with self.assertRaises(ProviderRoutingError) as ctx:
            resolve_route({"writer": _ROUTED}, "mystery_agent", "high")
        self.assertIn("mystery_agent", str(ctx.exception))

    def test_comment_keys_in_routing_are_not_mistaken_for_routes(self) -> None:
        """config.example.json carries __comment_* string keys in routing."""
        routing = {"__comment_routing_format": "prose", "default": _ROUTED}
        self.assertEqual(resolve_route(routing, "__comment_routing_format", "high"), "codex")

    def test_override_provider_bypasses_routing_entirely(self) -> None:
        selection = select_provider({}, agent="unrouted", significance="high", override_provider="mock")
        self.assertEqual(selection.provider, "mock")


class LocalPayloadTests(unittest.TestCase):
    def _payload(self, config: dict) -> dict:
        import json
        from unittest.mock import MagicMock, patch

        from lisan.providers.local import LocalClient

        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "ok"}}]}
        ).encode()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda s, *a: False
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            LocalClient(config).complete("hello")
        return json.loads(urlopen.call_args.args[0].data.decode("utf-8"))

    def test_null_default_model_omits_the_model_key(self) -> None:
        """Sending "model": null is not the same as saying nothing — it is
        what made mlx_lm.server drop the connection on 2026-08-16."""
        payload = self._payload(
            {"providers": {"local": {"base_url": "http://127.0.0.1:8080/v1/chat/completions", "default_model": None}}}
        )
        self.assertNotIn("model", payload)

    def test_configured_model_is_still_sent(self) -> None:
        payload = self._payload(
            {"providers": {"local": {"base_url": "http://127.0.0.1:8080/v1/chat/completions", "default_model": "demo"}}}
        )
        self.assertEqual(payload["model"], "demo")


if __name__ == "__main__":
    unittest.main()
