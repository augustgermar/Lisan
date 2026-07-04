from __future__ import annotations

import json
from typing import Any

from .base import PromptAgent


class VoiceExtractorAgent(PromptAgent):
    """Stage 2 of the voice extraction pass: distill candidate voice
    invariants from the agent's own transcript history. The model proposes;
    a deterministic evidence gate in tools/voice_extract.py disposes —
    every candidate must cite verbatim quotes that resolve to real turns."""

    name = "dreamer"  # reflection-class work shares the dreamer's routing
    prompt_file = "voice_extract_v1"
    output_schema_name = "voice_candidates"

    def fallback_output(self, user_input: str, significance: str = "medium", **kwargs: Any) -> str:
        # No provider → no candidates. The pass degrades to surface stats
        # only; it must never invent invariants deterministically.
        return json.dumps({"candidates": []})
