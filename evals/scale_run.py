"""Drive the generated life into the vault at scale, snapshotting metrics.

Runs each turn through the real chat pipeline and settles the background
memory jobs, so dedup / entity resolution / narrative growth are all
exercised. Every `--every` turns it records a metrics snapshot: entity and
record counts, response latency, and fragmentation signals (exact-duplicate
and near-duplicate entity names). Writes a JSONL metrics log so growth and
degradation over scale are visible.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VAULT = Path("/Users/august/.lisan/vault")
DB = Path("/Users/august/.lisan/repo/lisan.sqlite")


def _settle():
    from lisan.tools.jobs import run_jobs_worker

    for jt in ({"capture.observe"}, {"entity.rewrite_story"}):
        for _ in range(8):
            if not run_jobs_worker(vault=VAULT, db_path=DB, job_types=jt).get("processed_count"):
                break


def turn(conversation_id: str, text: str, settle: bool = True) -> float:
    from lisan.tools.chat import _process_chat_turn

    started = time.time()
    with contextlib.redirect_stdout(sys.stderr):
        _process_chat_turn(
            vault=VAULT, conversation_id=conversation_id, text=text,
            provider=None, model=None, db_path=DB, approval_fn=lambda n, a: True,
        )
        if settle:
            _settle()
    return round(time.time() - started, 1)


def _canonical_names() -> list[tuple[str, str]]:
    from lisan.frontmatter import load_markdown

    out = []
    for p in (VAULT / "entities").rglob("*.md"):
        try:
            fm = load_markdown(p).frontmatter
            name = str(fm.get("canonical_name") or "").strip()
            if name:
                out.append((name, p.stem))
        except Exception:
            continue
    return out


def _fragmentation(names: list[tuple[str, str]]) -> dict:
    lower = defaultdict(int)
    for name, _ in names:
        lower[name.lower()] += 1
    exact_dups = {k: v for k, v in lower.items() if v > 1}
    # near-duplicates: one canonical name is a token-subset of another
    near = []
    canon = sorted({n for n, _ in names})
    lows = [c.lower() for c in canon]
    for i, a in enumerate(lows):
        atoks = set(a.split())
        for b in lows[i + 1:]:
            btoks = set(b.split())
            if a != b and (atoks < btoks or btoks < atoks) and (atoks & btoks):
                near.append((a, b))
    return {"exact_duplicate_names": exact_dups, "near_duplicate_pairs": near[:50],
            "near_duplicate_count": len(near)}


def snapshot(turns_done: int, last_latency: float) -> dict:
    import sqlite3

    entity_files = list((VAULT / "entities").rglob("*.md"))
    total_records = [p for p in VAULT.rglob("*.md")
                     if "logs" not in p.parts and "transcripts" not in p.parts and "archive" not in p.parts]
    names = _canonical_names()
    try:
        conn = sqlite3.connect(DB)
        indexed = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        conn.close()
    except Exception:
        indexed = -1
    frag = _fragmentation(names)
    return {
        "turns_done": turns_done,
        "entities": len(entity_files),
        "total_md_records": len(total_records),
        "indexed_rows": indexed,
        "last_turn_latency_s": last_latency,
        "exact_dup_name_count": len(frag["exact_duplicate_names"]),
        "near_dup_pairs": frag["near_duplicate_count"],
        "ts": time.strftime("%H:%M:%S"),
    }


if __name__ == "__main__":
    import argparse

    from scale_life import generate

    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--metrics", type=Path, default=Path("/tmp/lisan-scale-metrics.jsonl"))
    ap.add_argument("--defer-capture", action="store_true",
                    help="Skip per-turn settle; queue captures and drain once at the end (fast).")
    args = ap.parse_args()

    life = generate(args.turns, seed=args.seed)
    conv = f"scale-{args.seed}"
    args.metrics.write_text("")
    settle_each = not args.defer_capture
    for i, text in enumerate(life, start=1):
        latency = turn(conv, text, settle=settle_each)
        if i % args.every == 0 or i == len(life):
            snap = snapshot(i, latency)
            with args.metrics.open("a") as f:
                f.write(json.dumps(snap) + "\n")
            print(f"[{snap['ts']}] turn {i}/{len(life)} | entities={snap['entities']} "
                  f"records={snap['total_md_records']} indexed={snap['indexed_rows']} "
                  f"latency={latency}s dup={snap['exact_dup_name_count']} near={snap['near_dup_pairs']}",
                  file=sys.stderr)
    if args.defer_capture:
        print("draining deferred capture backlog...", file=sys.stderr)
        t0 = time.time()
        _settle()
        print(f"drain took {round(time.time()-t0)}s", file=sys.stderr)
    print(json.dumps(snapshot(len(life), 0.0), indent=2))
