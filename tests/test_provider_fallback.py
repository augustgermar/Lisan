"""Provider fallback chains and the claude provider's request shape."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.providers.base import LLMResponse, LisanLLM, ProviderError
from lisan.providers.claude import ClaudeClient
from lisan.providers.config import ProviderSelection, fallback_chain


class FallbackChainTests(unittest.TestCase):
    def test_no_fallback_is_just_the_provider(self) -> None:
        self.assertEqual(fallback_chain({"providers": {"claude": {}}}, "claude"), ["claude"])
        self.assertEqual(fallback_chain({}, "codex"), ["codex"])

    def test_string_and_list_fallbacks_follow_through(self) -> None:
        cfg = {"providers": {"claude": {"fallback": "codex"}, "codex": {"fallback": ["local"]}}}
        self.assertEqual(fallback_chain(cfg, "claude"), ["claude", "codex", "local"])

    def test_cycle_terminates(self) -> None:
        cfg = {"providers": {"claude": {"fallback": "codex"}, "codex": {"fallback": "claude"}}}
        self.assertEqual(fallback_chain(cfg, "claude"), ["claude", "codex"])


class _Client:
    def __init__(self, name: str, fail: bool) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return LLMResponse(text=f"from {self.name}", provider=self.name, model="m")


class LisanLLMFallbackTests(unittest.TestCase):
    def _run(self, clients: dict, **kwargs):
        cfg = {"providers": {"claude": {"fallback": "codex", "default_model": "cm"}, "codex": {"default_model": "xm"}}}
        with tempfile.TemporaryDirectory() as tmp:
            llm = LisanLLM(config=cfg, db_path=Path(tmp) / "lisan.sqlite")
            with (
                patch("lisan.providers.base.select_provider", return_value=ProviderSelection(provider="claude", model="cm")),
                patch("lisan.providers.base._client_for", side_effect=lambda name, config: clients[name]),
                patch("time.sleep"),
            ):
                return llm.complete("hi", agent="writer", **kwargs)

    def test_primary_success_never_touches_fallback(self) -> None:
        clients = {"claude": _Client("claude", False), "codex": _Client("codex", False)}
        self.assertEqual(self._run(clients).text, "from claude")
        self.assertEqual(clients["codex"].calls, 0)

    def test_primary_failure_falls_back(self) -> None:
        clients = {"claude": _Client("claude", True), "codex": _Client("codex", False)}
        self.assertEqual(self._run(clients).text, "from codex")

    def test_all_failing_raises_the_last_error(self) -> None:
        clients = {"claude": _Client("claude", True), "codex": _Client("codex", True)}
        with self.assertRaisesRegex(ProviderError, "codex down"):
            self._run(clients)

    def test_explicit_provider_override_skips_fallback(self) -> None:
        clients = {"claude": _Client("claude", True), "codex": _Client("codex", False)}
        with self.assertRaisesRegex(ProviderError, "claude down"):
            self._run(clients, provider="claude")
        self.assertEqual(clients["codex"].calls, 0)


try:
    import claude_agent_sdk  # noqa: F401
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


@unittest.skipUnless(_HAS_SDK, "claude-agent-sdk not installed (pip install 'lisan[claude]')")
class ClaudeOptionsTests(unittest.TestCase):
    """The provider must stay a no-tools, no-settings, no-persistence call."""

    def _capture(self, cfg: dict) -> dict:
        import claude_agent_sdk

        seen: dict = {}

        async def fake_query(*, prompt, options):
            seen["options"], seen["prompt"] = options, prompt
            yield claude_agent_sdk.ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="s", result='{"a": 1}',
            )

        with patch("claude_agent_sdk.query", fake_query):
            ClaudeClient({"providers": {"claude": cfg}}).complete(
                "p", schema={"title": "t", "type": "object", "properties": {"a": {"type": "integer"}}},
            )
        return seen

    def test_locked_down_by_default(self) -> None:
        seen = self._capture({})
        o = seen["options"]
        self.assertEqual(o.tools, [])
        self.assertEqual(o.allowed_tools, [])
        self.assertEqual(o.setting_sources, [])
        self.assertEqual(o.max_turns, 1)
        self.assertIn("no-session-persistence", o.extra_args)
        self.assertEqual(o.model, "claude-sonnet-5-5")
        self.assertIn("valid JSON only", seen["prompt"])

    def test_tools_are_opt_in(self) -> None:
        o = self._capture({"allowed_tools": ["Read"]})["options"]
        self.assertEqual(o.tools, ["Read"])

    def test_error_result_raises_provider_error(self) -> None:
        import claude_agent_sdk

        async def fake_query(*, prompt, options):
            yield claude_agent_sdk.ResultMessage(
                subtype="error", duration_ms=1, duration_api_ms=1, is_error=True,
                num_turns=1, session_id="s", result="usage limit reached",
            )

        with patch("claude_agent_sdk.query", fake_query):
            with self.assertRaisesRegex(ProviderError, "usage limit"):
                ClaudeClient({"providers": {"claude": {}}}).complete("p")


if __name__ == "__main__":
    unittest.main()
