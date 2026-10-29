"""Consistency rubric, generated deterministically from the identity kernel.

One dimension per ratified voice invariant (1:1 — the rubric IS the kernel,
in judgeable form) plus four global dimensions. The rubric records the
kernel hash it was generated from; if the kernel is re-ratified, regenerate
the rubric and recapture the baseline (WO-3).
"""
from __future__ import annotations

import re
from pathlib import Path

from .kernel import kernel_path, kernel_voice_block, stored_kernel_hash

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


# Dimension ids are durable. They used to be the first four words of each
# voice statement, so re-ratifying the voice with any rewording renamed the
# dimension: its score history was orphaned and a self-repair bake check
# targeting it could no longer find a score. A registry in the vault now
# carries each id forward across rewordings.
REGISTRY_RELPATH = Path("self") / "rubric-dimensions.json"
REWORDING_SIMILARITY = 0.5


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def _similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _load_registry(vault: Path) -> list[dict]:
    import json

    path = vault / REGISTRY_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("dimensions") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and e.get("id") and e.get("statement")]


def _save_registry(vault: Path, entries: list[dict]) -> None:
    import json

    path = vault / REGISTRY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"dimensions": entries}, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _assign_ids(statements: list[str], registry: list[dict]) -> tuple[list[str], list[dict]]:
    """Exact statement → its id; a rewording (word-set Jaccard ≥ 0.5 with an
    unclaimed registered statement) → that statement's id; otherwise a new
    slug. Returns the ids and the updated registry."""
    registry = [dict(e) for e in registry]
    claimed: set[str] = set()
    used: set[str] = {str(e["id"]) for e in registry}
    ids: list[str] = []
    for statement in statements:
        match = next((e for e in registry if e["statement"] == statement and e["id"] not in claimed), None)
        if match is None:
            scored = sorted(
                ((_similarity(statement, e["statement"]), e["id"], e) for e in registry if e["id"] not in claimed),
                key=lambda t: (-t[0], t[1]),
            )
            if scored and scored[0][0] >= REWORDING_SIMILARITY:
                match = scored[0][2]
        if match is not None:
            claimed.add(match["id"])
            match["statement"] = statement
            match.pop("retired", None)
            ids.append(str(match["id"]))
            continue
        new_id = _slug(statement, used)
        registry.append({"id": new_id, "statement": statement})
        claimed.add(new_id)
        ids.append(new_id)
    for entry in registry:
        if entry["id"] not in claimed:
            entry["retired"] = True
    return ids, registry


def rubric_from_kernel(vault: Path, *, persist: bool = False) -> dict:
    """Deterministic: same kernel + same registry → same rubric.

    ``persist`` records new and reworded dimensions in the vault registry
    (self-eval passes it; a dry inspection does not)."""
    voice = kernel_voice_block(vault)
    statements = []
    for line in voice.splitlines():
        line = line.strip()
        if line.startswith("- ") and line[2:].strip():
            statements.append(line[2:].strip())
    registry = _load_registry(vault)
    ids, updated = _assign_ids(statements, registry)
    if persist and updated != registry:
        _save_registry(vault, updated)
    dimensions = [
        {"id": dim_id, "kind": "voice", "statement": statement}
        for dim_id, statement in zip(ids, statements)
    ]
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
    parser.add_argument("--vault", type=Path, default=Path.home() / ".lisan" / "vault")
    args = parser.parse_args()
    print(json.dumps(rubric_from_kernel(args.vault), indent=2, ensure_ascii=True))
