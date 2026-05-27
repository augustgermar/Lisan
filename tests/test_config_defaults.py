from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lisan.config import load_config


class ConfigDefaultsTests(unittest.TestCase):
    def test_default_local_provider_points_to_omnius(self) -> None:
        config = load_config()
        local = config["providers"]["local"]
        self.assertEqual(local["base_url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(
            local["default_model"],
            "/Users/august/code/omnius/models/Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2",
        )

    def test_legacy_local_provider_defaults_are_upgraded(self) -> None:
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
        self.assertEqual(
            local["default_model"],
            "/Users/august/code/omnius/models/Jiunsong/supergemma4-26b-uncensored-mlx-4bit-v2",
        )

