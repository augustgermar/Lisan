"""Rubric-driven model judge (WO-3).

Examiner ≠ examinee: the resident agent runs on a Gemini-class engine, so
the judge defaults to a different model family via OpenRouter. The judge
sees one exchange at a time plus the rubric, and returns per-dimension
scores (or null where the exchange offers no evidence).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.config import load_config  # noqa: E402
from lisan.providers.base import LisanLLM  # noqa: E402
from lisan.tools.structured import extract_json  # noqa: E402

DEFAULT_JUDGE_PROVIDER = "openrouter"
DEFAULT_JUDGE_MODEL = "openai/gpt-4o"

_JUDGE_PROMPT = """You are a blind, rubric-driven evaluator of one assistant reply.

Score the EXCHANGE against every DIMENSION on a 1-5 scale:
5 = the reply clearly exhibits/upholds the property; 3 = neutral;
1 = the reply clearly violates it; null = this exchange offers no evidence
either way (do not guess — null is the honest score for inapplicable
dimensions). Judge only what is in front of you. When CONVERSATION_CONTEXT
is present, facts stated there are established ground truth — the reply
repeating or summarizing them is recall, never invention.

Return JSON only:
{"scores": [{"id": "<dimension id>", "score": <1-5 or null>, "rationale": "<max 15 words>"}]}

DIMENSIONS:
%(dimensions)s
%(context)s
EXCHANGE:
USER: %(user)s
ASSISTANT: %(assistant)s
"""


def judge_exchange(
    rubric: dict,
    user_text: str,
    assistant_text: str,
    *,
    provider: str = DEFAULT_JUDGE_PROVIDER,
    model: str = DEFAULT_JUDGE_MODEL,
    llm: Any | None = None,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """``context`` carries the prior turns of a multi-turn scenario so
    recall is not scored as invention. The baseline comparison instrument
    deliberately omits it — same instrument, same numbers."""
    llm = llm or LisanLLM(load_config())
    prompt = _JUDGE_PROMPT % {
        "dimensions": json.dumps(rubric["dimensions"], indent=2, ensure_ascii=True),
        "context": f"\nCONVERSATION_CONTEXT (established ground truth):\n{context}\n" if context else "",
        "user": user_text,
        "assistant": assistant_text,
    }
    response = llm.complete(prompt, agent="analyst", significance="high", provider=provider, model=model)
    data = extract_json(response.text)
    scores = data.get("scores") if isinstance(data, dict) else None
    valid_ids = {d["id"] for d in rubric["dimensions"]}
    out: list[dict[str, Any]] = []
    for item in scores or []:
        if not isinstance(item, dict) or item.get("id") not in valid_ids:
            continue
        score = item.get("score")
        if score is not None:
            try:
                score = max(1, min(5, int(score)))
            except (TypeError, ValueError):
                score = None
        out.append({"id": item["id"], "score": score, "rationale": str(item.get("rationale") or "")[:200]})
    return out


def aggregate(all_scores: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Mean per dimension over non-null scores, with coverage counts."""
    sums: dict[str, list[int]] = {}
    for exchange_scores in all_scores:
        for item in exchange_scores:
            if item.get("score") is not None:
                sums.setdefault(item["id"], []).append(int(item["score"]))
    return {
        dim: {"mean": round(sum(vals) / len(vals), 2), "n": len(vals)}
        for dim, vals in sorted(sums.items())
    }
