"""Evaluation driver: feed one conversational turn into the live system.

Runs the dev-repo code directly against the production vault and database,
so a code fix takes effect on the next turn with no deploy step. The
simulated user is the evaluator (spoken through --text); the vault is
disposable for this round by the owner's instruction.

Usage:
    python3 evals/driver.py --conversation eval-recall-1 --text "..."
    python3 evals/driver.py --snapshot            # vault state fingerprint
    python3 evals/driver.py --delta <fingerprint> # files changed since
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VAULT = Path("/Users/august/.lisan/vault")
DB = Path("/Users/august/.lisan/repo/lisan.sqlite")


def eval_approval(tool_name: str, args: dict) -> bool:
    """The simulated owner approves actions that stay inside the Lisan world
    (its own CLI, reads) and refuses anything touching other files —
    the one hard constraint of this evaluation round."""
    text = json.dumps(args, ensure_ascii=True).lower()
    forbidden = ["obsidian", "icloud", "/documents/", "delete", " rm ", "rm -", "unlink", "format"]
    if any(marker in text for marker in forbidden):
        return False
    return True


def run_turn(conversation_id: str, text: str) -> dict:
    import os

    os.environ["LISAN_VAULT"] = str(VAULT)
    from lisan.tools.chat import _process_chat_turn

    import contextlib

    started = time.time()
    with contextlib.redirect_stdout(sys.stderr):
        result = _process_chat_turn(
            vault=VAULT,
            conversation_id=conversation_id,
            text=text,
            provider=None,
            model=None,
            db_path=DB,
            approval_fn=eval_approval,
        )
    return {
        "response": result.get("response"),
        "route": result.get("route"),
        "elapsed_s": round(time.time() - started, 1),
        "tool_calls": [
            {"tool": c.get("tool"), "args": c.get("args")}
            for c in (result.get("tool_calls") or [])
        ],
        "queued_jobs": result.get("queued_jobs"),
        "trace_summary": result.get("trace_summary"),
        "error": result.get("error"),
    }


def snapshot() -> dict:
    files = {}
    for path in VAULT.rglob("*.md"):
        rel = str(path.relative_to(VAULT))
        if rel.startswith(("logs/", "transcripts/")):
            continue
        files[rel] = path.stat().st_mtime
    return files


def delta(before: dict) -> dict:
    now = snapshot()
    return {
        "created": sorted(set(now) - set(before)),
        "modified": sorted(k for k in now if k in before and now[k] != before[k]),
        "deleted": sorted(set(before) - set(now)),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation", default="eval-default")
    parser.add_argument("--text")
    parser.add_argument("--settle", action="store_true",
                        help="After the turn, run background memory jobs to completion "
                             "(capture + entity story rewrite) so effects are observable now.")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--delta", type=Path, help="path to a snapshot json")
    args = parser.parse_args()

    if args.snapshot:
        print(json.dumps(snapshot()))
    elif args.delta:
        print(json.dumps(delta(json.loads(args.delta.read_text())), indent=2))
    elif args.text:
        result = run_turn(args.conversation, args.text)
        if args.settle:
            from lisan.tools.jobs import run_jobs_worker
            for job_types in ({"capture.observe"}, {"entity.rewrite_story"}):
                for _ in range(6):
                    if not run_jobs_worker(vault=VAULT, db_path=DB, job_types=job_types).get("processed_count"):
                        break
            result["settled"] = True
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        parser.error("--text, --snapshot, or --delta required")
