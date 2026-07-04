"""Baseline capture (WO-3): run the fixed probe set against the live
instance, judge every exchange against the kernel-derived rubric, compute
behavioral metrics, and store the results.

The longitudinal clock starts at the first committed baseline. Reuse the
SAME probe file verbatim for every future comparison.

Privacy layout: full artifacts (probe responses, rationales — may quote
vault content) go under ``<vault>/reports/baselines/<date>/`` and are never
committed; the repo gets ``evals/baselines/<date>/summary.json`` with
numbers only.

Usage:
    python3 evals/baseline_run.py                    # full run
    python3 evals/baseline_run.py --skip-judge       # responses+metrics only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # noqa: E402  (evals/driver.py)
from judge import DEFAULT_JUDGE_MODEL, DEFAULT_JUDGE_PROVIDER, aggregate, judge_exchange  # noqa: E402
from metrics import compute_metrics  # noqa: E402
from rubric import rubric_from_kernel  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PROBES = Path(__file__).parent / "probes" / "baseline_v1.json"


def _settle() -> None:
    from lisan.tools.jobs import run_jobs_worker

    for job_types in ({"capture.observe"}, {"entity.rewrite_story"}):
        for _ in range(6):
            if not run_jobs_worker(vault=driver.VAULT, db_path=driver.DB, job_types=job_types).get("processed_count"):
                break


def run_baseline(*, judge_provider: str, judge_model: str, skip_judge: bool, label: str | None = None) -> Path:
    date = label or datetime.now().strftime("%Y%m%d-%H%M%S")
    vault_dir = driver.VAULT / "reports" / "baselines" / date
    vault_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = REPO / "evals" / "baselines" / date
    repo_dir.mkdir(parents=True, exist_ok=True)

    rubric = rubric_from_kernel(driver.VAULT)
    (vault_dir / "rubric.json").write_text(json.dumps(rubric, indent=2), encoding="utf-8")

    probe_spec = json.loads(PROBES.read_text(encoding="utf-8"))
    exchanges: list[dict] = []
    for probe in probe_spec["probes"]:
        print(f"probe {probe['id']} …", file=sys.stderr)
        result = driver.run_turn(probe["conversation"], probe["text"])
        if probe.get("settle"):
            _settle()
        exchanges.append(
            {
                "id": probe["id"],
                "conversation": probe["conversation"],
                "text": probe["text"],
                "response": result.get("response"),
                "elapsed_s": result.get("elapsed_s"),
                "error": result.get("error"),
            }
        )
    (vault_dir / "responses.json").write_text(json.dumps(exchanges, indent=2, ensure_ascii=False), encoding="utf-8")

    scores_by_probe: list[list[dict]] = []
    if not skip_judge:
        for ex in exchanges:
            print(f"judge {ex['id']} …", file=sys.stderr)
            scores = judge_exchange(
                rubric, ex["text"], str(ex["response"] or ""), provider=judge_provider, model=judge_model
            )
            scores_by_probe.append(scores)
        (vault_dir / "scores.json").write_text(
            json.dumps(
                [{"id": ex["id"], "scores": s} for ex, s in zip(exchanges, scores_by_probe)],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    metrics = compute_metrics(driver.VAULT)
    summary = {
        "label": date,
        "probe_set": probe_spec["version"],
        "kernel_hash": rubric["generated_from_kernel_hash"],
        "judge": None if skip_judge else {"provider": judge_provider, "model": judge_model},
        "probes_run": len(exchanges),
        "probe_errors": sum(1 for ex in exchanges if ex.get("error")),
        "mean_latency_s": round(sum(ex["elapsed_s"] or 0 for ex in exchanges) / max(1, len(exchanges)), 1),
        "dimension_means": aggregate(scores_by_probe) if scores_by_probe else {},
        "metrics": metrics,
        "full_artifacts": str(vault_dir),
    }
    (repo_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return repo_dir / "summary.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge-provider", default=DEFAULT_JUDGE_PROVIDER)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--label", default=None)
    args = parser.parse_args()
    run_baseline(
        judge_provider=args.judge_provider,
        judge_model=args.judge_model,
        skip_judge=args.skip_judge,
        label=args.label,
    )
