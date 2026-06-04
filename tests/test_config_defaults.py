from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.config import load_config


class ConfigDefaultsTests(unittest.TestCase):
    def test_default_local_provider_config(self) -> None:
        config = load_config()
        local = config["providers"]["local"]
        self.assertEqual(local["base_url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertIsNone(local["default_model"])

    def test_legacy_ollama_base_url_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "local": {
                                "base_url": "http://localhost:11434/v1/chat/completions",
                                "default_model": "llama3.1",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(path)

        local = config["providers"]["local"]
        self.assertEqual(local["base_url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(local["default_model"], "llama3.1")

