from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..paths import repo_root
from ..tools.structured import extract_json
from .base import LLMResponse, ProviderClient, ProviderError


class CodexClient(ProviderClient):
    name = "codex"

    def complete(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        agent: str = "writer",
        significance: str = "medium",
        model: str | None = None,
    ) -> LLMResponse:
        binary = os.getenv(self.config["providers"]["codex"].get("binary_env") or "", "codex")
        chosen_model = model or self.config["providers"]["codex"].get("default_model") or None
        if not binary:
            raise ProviderError("CODEX_BIN is empty")

        args = [binary, "exec", "--skip-git-repo-check", "--cd", str(repo_root())]
        if chosen_model:
            args.extend(["--model", chosen_model])

        schema_path: Path | None = None
        output_path: Path | None = None
        try:
            if schema:
                with tempfile.NamedTemporaryFile("w", suffix=".schema.json", delete=False) as schema_file:
                    json.dump(schema, schema_file, indent=2)
                    schema_path = Path(schema_file.name)
                args.extend(["--output-schema", str(schema_path)])

            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as output_file:
                output_path = Path(output_file.name)
            args.extend(["--output-last-message", str(output_path)])
            args.append("-")

            proc = subprocess.run(args, input=prompt, capture_output=True, text=True)
            if proc.returncode != 0:
                raise ProviderError(
                    "codex exec failed with exit code "
                    f"{proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
                )

            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                text = proc.stdout.strip()
            if schema:
                parsed = extract_json(text)
                if not isinstance(parsed, dict):
                    raise ProviderError("codex did not return JSON matching the requested schema")
                text = json.dumps(parsed, indent=2, ensure_ascii=True)
            return LLMResponse(text=text, provider=self.name, model=chosen_model or "", raw={"stdout": proc.stdout, "stderr": proc.stderr})
        finally:
            if schema_path and schema_path.exists():
                schema_path.unlink(missing_ok=True)
            if output_path and output_path.exists():
                output_path.unlink(missing_ok=True)
