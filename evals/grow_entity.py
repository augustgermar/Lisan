"""Grow one entity through many turns and snapshot its narrative at each stage.

Feeds a sequence of conversation turns that add information about a single
entity, drains the capture + story-rewrite jobs synchronously after each, and
records the entity narrative's length and shape as it accretes — to observe
how the writer structures a story as its complexity increases.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VAULT = Path("/Users/august/.lisan/vault")
DB = Path("/Users/august/.lisan/repo/lisan.sqlite")


def _drain(job_types: set[str]) -> None:
    from lisan.tools.jobs import run_jobs_worker

    for _ in range(6):
        summary = run_jobs_worker(vault=VAULT, db_path=DB, job_types=job_types)
        if not summary.get("processed_count"):
            break


def turn(conversation_id: str, text: str) -> str:
    import contextlib

    from lisan.tools.chat import _process_chat_turn

    with contextlib.redirect_stdout(sys.stderr):
        result = _process_chat_turn(
            vault=VAULT, conversation_id=conversation_id, text=text,
            provider=None, model=None, db_path=DB,
            approval_fn=lambda n, a: True,
        )
        _drain({"capture.observe"})
        _drain({"entity.rewrite_story"})
    return str(result.get("response") or "")


def find_entity(slug_hint: str) -> Path | None:
    # exact stem preferred; fall back to prefix so we never grab a different
    # entity that merely contains the hint ("silas-s-kelp-nonprofit").
    candidates = list((VAULT / "entities").rglob("*.md"))
    for p in candidates:
        if p.stem.lower() == slug_hint.lower():
            return p
    for p in candidates:
        if p.stem.lower().startswith(slug_hint.lower()):
            return p
    return None


def narrative_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    body = parts[2] if len(parts) >= 3 else text
    body = re.sub(r"^#\s+.*$", "", body, count=1, flags=re.M).strip()
    return body


def shape(body: str) -> dict:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    words = len(body.split())
    return {
        "words": words,
        "paragraphs": len(paras),
        "para_word_counts": [len(p.split()) for p in paras],
        "chars": len(body),
    }


if __name__ == "__main__":
    scenario = json.loads(Path(sys.argv[1]).read_text())
    conv = scenario["conversation_id"]
    slug = scenario["entity_slug_hint"]
    checkpoints = set(scenario.get("checkpoints", []))
    out = []
    for i, msg in enumerate(scenario["turns"], start=1):
        turn(conv, msg)
        if i in checkpoints or i == len(scenario["turns"]):
            path = find_entity(slug)
            if path:
                body = narrative_of(path)
                out.append({"turn": i, "shape": shape(body), "narrative": body})
                print(f"--- after turn {i}: {shape(body)}", file=sys.stderr)
            else:
                print(f"--- after turn {i}: entity not found", file=sys.stderr)
        time.sleep(0.3)
    print(json.dumps(out, indent=2, ensure_ascii=False))
