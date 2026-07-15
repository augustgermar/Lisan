from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.providers.base import LLMResponse, LisanLLM, ProviderError
from lisan.providers.config import ProviderSelection


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("HTTP 429 from http://example.test: rate limited")
        return LLMResponse(text="ok", provider="local", model="demo")


class ProviderRetryTests(unittest.TestCase):
    def test_llm_retries_transient_provider_error_then_succeeds(self) -> None:
        fake = _FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lisan.sqlite"
            llm = LisanLLM(config={}, db_path=db_path)
            with (
                patch("lisan.providers.base.select_provider", return_value=ProviderSelection(provider="local", model="demo")),
                patch("lisan.providers.base._client_for", return_value=fake),
                patch("time.sleep") as sleep,
            ):
                response = llm.complete("hello", agent="writer", significance="medium")

        self.assertEqual(response.text, "ok")
        self.assertEqual(fake.calls, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()


class CodexSandboxTests(unittest.TestCase):
    """Non-executor agents run codex read-only: the codex CLI is agentic and
    will sometimes act inline despite instructions, which would bypass the
    run_codex approval gate. The boundary must be structural."""

    def _args_for(self, agent: str, sandbox_mode: str | None = None) -> list[str]:
        return self._capture_args(agent, sandbox_mode=sandbox_mode)

    def _capture_args(self, agent: str, sandbox_mode: str | None = None) -> list[str]:
        from unittest.mock import MagicMock

        from lisan.providers.codex import CodexClient

        codex_cfg = {"sandbox_mode": sandbox_mode} if sandbox_mode else {}
        client = CodexClient({"providers": {"codex": codex_cfg}})
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(list(args))
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = "ok"
            proc.stderr = ""
            return proc

        with patch("lisan.providers.codex.subprocess.run", side_effect=fake_run):
            client.complete("hello", agent=agent)
        return captured[0]

    def test_decision_agents_run_read_only(self) -> None:
        for agent in ("interlocutor", "listener", "writer", "skeptic"):
            args = self._capture_args(agent)
            self.assertIn("--sandbox", args, f"{agent} must run sandboxed")
            self.assertIn("read-only", args)

    def test_executor_agent_default_is_unsandboxed_and_config_reversible(self):
        """Owner decision 2026-07-06: the executor runs with the sandbox
        bypassed by default (network-dependent tasks kept failing with
        misleading errors); setting providers.codex.sandbox_mode restores
        the cage. Non-executor agents stay read-only regardless."""
        args = self._args_for(agent="codex")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn("--sandbox", args)
        args = self._args_for(agent="codex", sandbox_mode="workspace-write")
        self.assertIn("--sandbox", args)
        self.assertIn("workspace-write", args)


class CodexBinaryErrorTests(unittest.TestCase):
    def test_missing_binary_raises_provider_error(self) -> None:
        """A missing codex binary must surface as ProviderError, not a raw
        FileNotFoundError — the raw OSError bypasses the provider-failure
        path and the user gets silence instead of an honest error."""
        import os

        from lisan.providers.codex import CodexClient

        config = {"providers": {"codex": {"binary_env": "LISAN_TEST_CODEX_BIN"}}}
        with patch.dict("os.environ", {"LISAN_TEST_CODEX_BIN": "/nonexistent/codex-test-binary"}, clear=False):
            client = CodexClient(config=config)
            with self.assertRaises(ProviderError) as ctx:
                client.complete("hello")
        self.assertIn("could not be launched", str(ctx.exception))


class RotatoClientTests(unittest.TestCase):
    def _client(self, body: dict):
        from unittest.mock import MagicMock, patch as _patch

        from lisan.providers.rotato import RotatoClient

        client = RotatoClient({"providers": {"rotato": {"default_model": "gemini-2.5-pro"}}})
        response = MagicMock()
        response.read.return_value = __import__("json").dumps(body).encode()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda s, *a: False
        return client, _patch("urllib.request.urlopen", return_value=response)

    def test_parses_openai_shape(self):
        client, ctx = self._client({"choices": [{"message": {"content": '{"response": "hi"}'}}]})
        with ctx:
            out = client.complete("x", schema={"type": "object"})
        self.assertIn("hi", out.text)
        self.assertEqual(out.provider, "rotato")

    def test_empty_message_raises_provider_error(self):
        client, ctx = self._client({"choices": [{"message": {"content": ""}}]})
        with ctx:
            with self.assertRaises(ProviderError):
                client.complete("x")

    def test_truncated_response_raises_instead_of_returning_half_json(self):
        # The 2026-07-06..12 defect: the proxy cut long writer output at its
        # default token ceiling, finish_reason "length", and the half-JSON
        # went on to a silent fallback that dropped the turn's records. A
        # truncated response is a provider failure, not a result.
        client, ctx = self._client({
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{\n  "entities_to_create": [\n    {"name": "Ti'},
            }]
        })
        with ctx:
            with self.assertRaises(ProviderError) as err:
                client.complete("x")
        self.assertIn("truncated", str(err.exception))

    def test_request_carries_explicit_max_tokens(self):
        client, ctx = self._client({"choices": [{"message": {"content": "ok"}}]})
        with ctx as urlopen:
            client.complete("x")
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertGreaterEqual(int(body["max_tokens"]), 16384)
