"""Capstone loop runner (WO-9): execute a persona scenario against the live
system, with simulated time between sessions, and dump everything the
reviewing agent needs — responses, vault deltas, drive markers, judge
scores — into one review bundle.

Assessment stays with the reviewer (transcripts AND vault artifacts,
never the transcript alone, per evals/EVALUATION_LOOP.md); this runner
just makes a cycle cheap to execute and impossible to under-observe.

Scenario file shape (evals/scenarios/*.json):
{
  "name": "returning-user",
  "steps": [
    {"turn": {"conversation": "cap-r1", "text": "..."}},
    {"settle": true},
    {"timeshift_days": 7},
    {"note": "expect: callback about the fence loop, question-phrased"}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver  # noqa: E402
from judge import aggregate, judge_exchange  # noqa: E402
from metrics import compute_metrics  # noqa: E402
from rubric import rubric_from_kernel  # noqa: E402
from timeshift import shift_vault  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _settle() -> None:
    from lisan.tools.jobs import run_jobs_worker

    for job_types in ({"capture.observe"}, {"entity.rewrite_story"}):
        for _ in range(6):
            if not run_jobs_worker(vault=driver.VAULT, db_path=driver.DB, job_types=job_types).get("processed_count"):
                break


def run_scenario(path: Path, *, judge: bool, judge_provider: str, judge_model: str) -> Path:
    scenario = json.loads(Path(path).read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = driver.VAULT / "reports" / "capstone" / f"{scenario['name']}-{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    log_path = driver.VAULT / "logs" / "lisan.log"
    log_before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    snapshot_before = driver.snapshot()

    rubric = rubric_from_kernel(driver.VAULT)
    events: list[dict] = []
    for step in scenario["steps"]:
        if "turn" in step:
            turn = step["turn"]
            print(f"[{scenario['name']}] turn {turn['conversation']}: {turn['text'][:60]!r}", file=sys.stderr)
            result = driver.run_turn(turn["conversation"], turn["text"])
            events.append({"kind": "turn", "conversation": turn["conversation"],
                           "text": turn["text"], "response": result.get("response"),
                           "elapsed_s": result.get("elapsed_s"), "error": result.get("error"),
                           "tool_calls": result.get("tool_calls")})
        elif step.get("settle"):
            _settle()
            events.append({"kind": "settle"})
        elif "timeshift_days" in step:
            shifted = shift_vault(driver.VAULT, int(step["timeshift_days"]), allow_live=True)
            events.append({"kind": "timeshift", **shifted})
        elif "note" in step:
            events.append({"kind": "note", "note": step["note"]})

    scores = []
    if judge:
        for ev in [e for e in events if e["kind"] == "turn" and e.get("response")]:
            scores.append({"conversation": ev["conversation"],
                           "scores": judge_exchange(rubric, ev["text"], str(ev["response"]),
                                                    provider=judge_provider, model=judge_model)})

    log_after = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    new_markers = [l for l in log_after[len(log_before):].splitlines() if "drive." in l]

    bundle = {
        "scenario": scenario["name"],
        "stamp": stamp,
        "events": events,
        "drive_markers": new_markers,
        "vault_delta": driver.delta(snapshot_before),
        "dimension_means": aggregate([s["scores"] for s in scores]) if scores else {},
        "scores": scores,
        "metrics": compute_metrics(driver.VAULT),
    }
    (outdir / "bundle.json").write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    print(str(outdir / "bundle.json"))
    return outdir / "bundle.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--judge-provider", default="openrouter")
    parser.add_argument("--judge-model", default="openai/gpt-4o")
    args = parser.parse_args()
    run_scenario(args.scenario, judge=args.judge,
                 judge_provider=args.judge_provider, judge_model=args.judge_model)
