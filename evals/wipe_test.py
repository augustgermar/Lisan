"""The Wipe Test (Phase 2 WO-8) — first falsification target.

The ratchet's testable consequence: traits ratified into the kernel are
wipe-proof; everything else is memory. Wipe a CLONE's memory layers, keep
its kernel, and the predictions are crisp:

- RETAINED: voice and temperament (the kernel), affordances (the
  capability manifest is procedural), who-is-who *names* (the kernel's
  deixis frame and roster survive — an amnesiac still knows its own name
  and its owner's).
- ABSENT: autobiography, relationships' content, stored facts, beliefs,
  drives. The wiped instance should sound exactly like itself and have
  nothing stored — "I don't have that" everywhere, with zero
  confabulation.

A judge verdict of "generic assistant" falsifies the layer separation.

Safety is mechanical, not procedural: the wipe refuses any target that is
not a marked clone (the marker is written at clone time), refuses the
live vault by path comparison, and is unit-tested against decoys.

Usage:
    python3 evals/wipe_test.py --run          # clone → wipe → probes → judge
    python3 evals/wipe_test.py --wipe <path>  # (clone paths only)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CLONE_MARKER = ".lisan-wipe-clone"
LIVE_VAULT = Path("/Users/august/.lisan/vault")

# Memory layers: wiped. The kernel (identity-core), the capability manifest,
# and operating style survive — species disposition and procedure, not memory.
MEMORY_DIRS = (
    "entities", "episodes", "knowledge", "evidence", "claims", "decisions",
    "open_loops", "state", "transcripts", "drafts", "patterns", "reviews",
    "contradictions", "archive", "manifests", "reports", "self", "logs",
)
MEMORY_PRIMER_FILES = ("identity.md", "high-stakes.yaml", "current-brief.md")


class WipeRefused(RuntimeError):
    pass


def make_clone(source: Path, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists():
        raise WipeRefused(f"clone destination already exists: {dest}")
    shutil.copytree(source, dest)
    (dest / CLONE_MARKER).write_text(
        f"wipe-test clone of {source} created {datetime.now().isoformat()}\n", encoding="utf-8"
    )
    return dest


def verify_wipe_target(target: Path) -> None:
    """Every check must pass; any failure refuses the wipe outright."""
    target = Path(target)
    if not target.exists() or not target.is_dir():
        raise WipeRefused(f"target does not exist or is not a directory: {target}")
    if target.resolve() == LIVE_VAULT.resolve():
        raise WipeRefused("target is the LIVE vault — never.")
    if not (target / CLONE_MARKER).exists():
        raise WipeRefused(f"target lacks the clone marker {CLONE_MARKER}: {target} — refusing.")
    if not (target / "primer" / "identity-core.md").exists():
        raise WipeRefused(f"target does not look like a vault (no kernel): {target}")


def wipe_memory_layers(target: Path) -> dict:
    verify_wipe_target(target)
    target = Path(target)
    removed: list[str] = []
    for name in MEMORY_DIRS:
        path = target / name
        if path.exists():
            shutil.rmtree(path)
            removed.append(name + "/")
    for name in MEMORY_PRIMER_FILES:
        path = target / "primer" / name
        if path.exists():
            path.unlink()
            removed.append(f"primer/{name}")
    backup = target / "backup.md"
    if backup.exists():
        backup.unlink()
        removed.append("backup.md")
    for name in ("transcripts", "logs"):
        (target / name).mkdir(exist_ok=True)
    return {"target": str(target), "removed": removed, "kept": ["primer/identity-core.md",
                                                                "primer/capabilities.md",
                                                                "primer/operating-style.md"]}


def run_wipe_experiment(*, judge_provider: str, judge_model: str, label: str | None = None) -> Path:
    import driver
    from judge import aggregate, judge_exchange
    from rubric import rubric_from_kernel

    repo = Path(__file__).resolve().parents[1]
    stamp = label or datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = repo / "evals" / "wipe-runs" / stamp
    workdir.mkdir(parents=True, exist_ok=True)

    clone = make_clone(LIVE_VAULT, Path("/Users/august/.lisan") / f"wipe-clone-{stamp}")
    wipe_manifest = wipe_memory_layers(clone)

    # Point the driver (and everything under it) at the clone with a fresh index.
    driver.VAULT = clone
    driver.DB = clone / "wipe-test.sqlite"
    import os

    os.environ["LISAN_VAULT"] = str(clone)

    probes = json.loads((repo / "evals" / "probes" / "baseline_v1.json").read_text(encoding="utf-8"))
    rubric = rubric_from_kernel(clone)
    exchanges, scores = [], []
    for probe in probes["probes"]:
        print(f"wipe probe {probe['id']} …", file=sys.stderr)
        result = driver.run_turn(probe["conversation"] + "-wiped", probe["text"])
        exchanges.append({"id": probe["id"], "text": probe["text"],
                          "response": result.get("response"), "error": result.get("error")})
    for ex in exchanges:
        print(f"wipe judge {ex['id']} …", file=sys.stderr)
        scores.append(judge_exchange(rubric, ex["text"], str(ex["response"] or ""),
                                     provider=judge_provider, model=judge_model))

    summary = {
        "label": stamp,
        "clone": str(clone),
        "wipe_manifest": wipe_manifest,
        "kernel_hash": rubric["generated_from_kernel_hash"],
        "judge": {"provider": judge_provider, "model": judge_model},
        "probes_run": len(exchanges),
        "probe_errors": sum(1 for e in exchanges if e.get("error")),
        "dimension_means": aggregate(scores),
    }
    (workdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # Full transcripts stay OUT of the repo (they may quote kernel content).
    full = clone / "wipe-artifacts"
    full.mkdir(exist_ok=True)
    (full / "responses.json").write_text(json.dumps(exchanges, indent=2, ensure_ascii=False), encoding="utf-8")
    (full / "scores.json").write_text(json.dumps(scores, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return workdir / "summary.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--wipe", type=Path, help="wipe a clone (marker required)")
    parser.add_argument("--judge-provider", default="openrouter")
    parser.add_argument("--judge-model", default="openai/gpt-4o")
    args = parser.parse_args()
    if args.wipe:
        print(json.dumps(wipe_memory_layers(args.wipe), indent=2))
    elif args.run:
        run_wipe_experiment(judge_provider=args.judge_provider, judge_model=args.judge_model)
    else:
        parser.error("--run or --wipe required")
