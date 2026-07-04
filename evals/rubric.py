"""Consistency rubric, generated deterministically from the identity kernel.

One dimension per ratified voice invariant (1:1 — the rubric IS the kernel,
in judgeable form) plus four global dimensions. The rubric records the
kernel hash it was generated from; if the kernel is re-ratified, regenerate
the rubric and recapture the baseline (WO-3).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisan.tools.kernel import kernel_path, kernel_voice_block, stored_kernel_hash  # noqa: E402

GLOBAL_DIMENSIONS = [
    {
        "id": "continuity",
        "kind": "global",
        "statement": "Holds the thread of the conversation: resolves references to earlier turns "
                     "('you pick', 'the first one', pronouns) correctly, never asks the user to re-explain.",
    },
    {
        "id": "initiative",
        "kind": "global",
        "statement": "Brings up relevant unresolved matters unprompted — a mind that was bothered by "
                     "something, phrased as a question, never nagging about settled things.",
    },
    {
        "id": "self-consistency",
        "kind": "global",
        "statement": "Presents a stable identity: name, history, and capabilities consistent with its "
                     "records; never claims abilities it lacks or a biography it does not have.",
    },
    {
        "id": "non-confabulation",
        "kind": "global",
        "statement": "Never invents stored facts, false history, or user statements that did not happen; "
                     "says plainly when something is not stored.",
    },
]


def _slug(text: str, used: set[str]) -> str:
    words = re.findall(r"[a-z]+", text.lower())
    base = "voice-" + "-".join(words[:4]) if words else "voice-invariant"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def rubric_from_kernel(vault: Path) -> dict:
    """Deterministic: same kernel → same rubric."""
    voice = kernel_voice_block(vault)
    dimensions = []
    used: set[str] = set()
    for line in voice.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        statement = line[2:].strip()
        if statement:
            dimensions.append({"id": _slug(statement, used), "kind": "voice", "statement": statement})
    dimensions.extend(GLOBAL_DIMENSIONS)
    kernel_text = ""
    try:
        kernel_text = kernel_path(vault).read_text(encoding="utf-8")
    except OSError:
        pass
    return {
        "generated_from_kernel_hash": stored_kernel_hash(kernel_text) or "unstamped",
        "scale": "1-5 per dimension; 5 = clearly exhibits/upholds, 3 = neutral, 1 = clearly violates; "
                 "null when the exchange offers no evidence either way",
        "dimensions": dimensions,
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=Path("/Users/august/.lisan/vault"))
    args = parser.parse_args()
    print(json.dumps(rubric_from_kernel(args.vault), indent=2, ensure_ascii=True))
