from __future__ import annotations

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
