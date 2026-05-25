from __future__ import annotations

import json
from datetime import datetime, timezone
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .live_oracle import RunEvaluation


def write_run_report(run_result: dict[str, Any], evaluation: RunEvaluation, report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    markdown = _render_run_markdown(run_result, evaluation)
    json_payload = _render_run_json(run_result, evaluation)
    md_path = report_dir / "report.md"
    json_path = report_dir / "report.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return {"markdown": md_path, "json": json_path}


def write_aggregate_report(
    run_results: list[dict[str, Any]],
    evaluations: list[RunEvaluation],
    output_dir: Path,
    cycle_runs: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / "aggregate.md"
    json_path = output_dir / "aggregate.json"
    cycles_path = output_dir / "cycles.json"
    markdown = _render_aggregate_markdown(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)
    json_payload = _render_aggregate_json(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)
    cycles_payload = _render_cycles_json(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    cycles_path.write_text(json.dumps(cycles_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return {"markdown": md_path, "json": json_path, "cycles": cycles_path}


def _render_run_markdown(run_result: dict[str, Any], evaluation: RunEvaluation) -> str:
    lines: list[str] = []
    run_id = str(run_result.get("run_id") or "")
    lines.append(f"# Live Eval Run {run_id}")
    lines.append("")
    lines.append("## Run Metadata")
    lines.append("")
    lines.append(f"- run_id: `{run_id}`")
    if run_result.get("cycle_index") is not None:
        lines.append(f"- cycle_index: `{run_result.get('cycle_index')}`")
    if run_result.get("seed") is not None:
        lines.append(f"- seed: `{run_result.get('seed')}`")
    lines.append(f"- created_at: `{run_result.get('created_at')}`")
    lines.append(f"- vault: `{run_result.get('vault')}`")
    lines.append(f"- db_path: `{run_result.get('db_path')}`")
    lines.append(f"- scenarios: `{', '.join(run_result.get('scenarios') or [])}`")
    if run_result.get("wiped") is not None:
        lines.append(f"- wiped: `{run_result.get('wiped')}`")
    lines.append(f"- passed: `{evaluation.passed}`")
    lines.append("")
    lines.append("## Provider")
    provider_info = run_result.get("provider_info") or {}
    if provider_info:
        for key, value in provider_info.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- unavailable")
    lines.append("")
    lines.append("## Scenario Summary")
    for scenario_run in run_result.get("scenario_runs") or []:
        lines.append(f"- {scenario_run.get('scenario')}: passed={scenario_run.get('passed')}, turns={len(scenario_run.get('turns') or [])}, durable_records={scenario_run.get('durable_records_total', 0)}, jobs_run={scenario_run.get('jobs_run_total', 0)}")
    lines.append("")
    lines.append("## Transcript")
    transcript = run_result.get("transcript_md") or ""
    lines.append(transcript.rstrip() or "_No transcript available._")
    lines.append("")
    lines.append("## Pass / Fail / Warning")
    lines.append(f"- passes: {run_result.get('pass_count', 0)}")
    lines.append(f"- failures: {run_result.get('fail_count', 0)}")
    lines.append(f"- warnings: {run_result.get('warning_count', 0)}")
    lines.append("")
    lines.append("## Latency Summary")
    latency = run_result.get("latency_summary") or {}
    for key, value in latency.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## LLM Call Summary")
    llm_summary = run_result.get("llm_summary") or {}
    for key, value in llm_summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Job Summary")
    job_summary = run_result.get("job_summary") or {}
    for key, value in job_summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Memory Records Created")
    for item in run_result.get("durable_records") or []:
        lines.append(f"- {item.get('path')} ({item.get('record_type')})")
    if not run_result.get("durable_records"):
        lines.append("- none")
    lines.append("")
    lines.append("## Retrieval Summary")
    retrieval = run_result.get("retrieval_summary") or {}
    for key, value in retrieval.items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Unexpected Positives")
    for item in run_result.get("positive_surprises") or []:
        lines.append(f"- {item.get('scenario')} turn {item.get('turn_number')}: {item.get('observed_behavior')}")
    if not run_result.get("positive_surprises"):
        lines.append("- none")
    lines.append("")
    lines.append("## Unexpected Negatives")
    for item in run_result.get("negative_surprises") or []:
        lines.append(f"- {item.get('scenario')} turn {item.get('turn_number')}: {item.get('observed_behavior')} | cause={item.get('likely_cause')}")
    if not run_result.get("negative_surprises"):
        lines.append("- none")
    lines.append("")
    lines.append("## Proposed Improvements")
    for item in run_result.get("proposed_improvements") or []:
        lines.append(f"- [{item.get('priority')}] {item.get('summary')} ({item.get('affected_subsystem')})")
    if not run_result.get("proposed_improvements"):
        lines.append("- none")
    lines.append("")
    lines.append("## Cleanup Status")
    lines.append(f"- cleanup_status: `{run_result.get('cleanup_status')}`")
    return "\n".join(lines).rstrip() + "\n"


def _render_run_json(run_result: dict[str, Any], evaluation: RunEvaluation) -> dict[str, Any]:
    payload = dict(run_result)
    payload["evaluation"] = evaluation.to_dict()
    return payload


def _render_aggregate_markdown(
    run_results: list[dict[str, Any]],
    evaluations: list[RunEvaluation],
    *,
    cycle_runs: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    payload = _build_aggregate_payload(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)
    lines: list[str] = ["# Live Eval Aggregate", ""]
    lines.append("## Status")
    lines.append(f"- status: `{payload.get('status')}`")
    if payload.get("failure_classification"):
        lines.append(f"- failure_classification: `{payload.get('failure_classification')}`")
    lines.append("")
    if payload.get("provider_diagnostics"):
        lines.append("## Provider Diagnostics")
        for key, value in payload["provider_diagnostics"].items():
            if key == "errors" and not value:
                lines.append("- errors: `[]`")
                continue
            if key == "suggested_fixes" and not value:
                lines.append("- suggested_fixes: `[]`")
                continue
            lines.append(f"- {key}: `{value}`")
        lines.append("")
    lines.append("## Runs")
    if payload["cycle_runs"]:
        for cycle in payload["cycle_runs"]:
            lines.append(
                f"- {cycle['run_id']}: passed={cycle['passed']}, warnings={cycle['warning_count']}, "
                f"scenarios={', '.join(cycle.get('scenarios') or [])}, wiped={cycle.get('wiped')}"
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Totals")
    lines.append(f"- cycles: `{payload['cycle_count']}`")
    lines.append(f"- passes: `{payload['totals']['pass_count']}`")
    lines.append(f"- failures: `{payload['totals']['fail_count']}`")
    lines.append(f"- provider_failures: `{payload.get('provider_failure_count', 0)}`")
    lines.append(f"- warnings: `{payload['totals']['warning_count']}`")
    lines.append("")
    lines.append("## Recurring Failures")
    _append_bullet_list(lines, payload["recurring_failures"])
    lines.append("")
    lines.append("## Recurring Surprises")
    _append_bullet_list(lines, payload["recurring_surprises"])
    lines.append("")
    lines.append("## Recurring Latency Issues")
    _append_bullet_list(lines, payload["recurring_latency_issues"])
    lines.append("")
    lines.append("## Recurring Identity / Perspective Problems")
    _append_bullet_list(lines, payload["recurring_identity_perspective_problems"])
    lines.append("")
    lines.append("## Slowest Turns")
    if payload["slowest_turns"]:
        for item in payload["slowest_turns"]:
            lines.append(
                f"- {item['run_id']} / {item['scenario']} turn {item['turn_number']}: "
                f"{item['elapsed_ms']} ms | {item['user_input']}"
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## LLM Call Summary")
    for key, value in payload["llm_call_summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Job Failure Summary")
    _append_bullet_list(lines, payload["job_failure_summary"].get("entries", []))
    lines.append("")
    lines.append("## Top Proposed Changes")
    _append_bullet_list(
        lines,
        [
            f"{item.get('summary')} [{item.get('priority')}] ({item.get('affected_subsystem')})"
            for item in payload["top_proposed_changes"]
        ],
    )
    lines.append("")
    lines.append("## Cleanup Status")
    for cycle in payload["cycle_runs"]:
        lines.append(f"- {cycle['run_id']}: {cycle.get('cleanup_status')}")
    return "\n".join(lines).rstrip() + "\n"


def _render_aggregate_json(
    run_results: list[dict[str, Any]],
    evaluations: list[RunEvaluation],
    *,
    cycle_runs: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _build_aggregate_payload(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)


def _render_cycles_json(
    run_results: list[dict[str, Any]],
    evaluations: list[RunEvaluation],
    *,
    cycle_runs: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _build_aggregate_payload(run_results, evaluations, cycle_runs=cycle_runs, meta=meta)
    return {
        "created_at": payload["created_at"],
        "cycle_count": payload["cycle_count"],
        "run_ids": payload["run_ids"],
        "cycles": payload["cycle_runs"],
    }


def _build_aggregate_payload(
    run_results: list[dict[str, Any]],
    evaluations: list[RunEvaluation],
    *,
    cycle_runs: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cycle_runs_payload: list[dict[str, Any]] = []
    if cycle_runs:
        for cycle, evaluation in zip(cycle_runs, evaluations, strict=False):
            payload = cycle.to_dict() if hasattr(cycle, "to_dict") else dict(cycle)
            payload.setdefault("passed", evaluation.passed if evaluation is not None else payload.get("passed"))
            payload.setdefault("warning_count", int(payload.get("warning_count") or 0))
            payload.setdefault("fail_count", int(payload.get("fail_count") or 0))
            payload.setdefault("pass_count", int(payload.get("pass_count") or 0))
            cycle_runs_payload.append(payload)
    if not cycle_runs_payload:
        cycle_runs_payload = [_cycle_summary_from_result(run_result, evaluation) for run_result, evaluation in zip(run_results, evaluations, strict=False)]

    all_failures: Counter[str] = Counter()
    all_surprises: Counter[str] = Counter()
    all_latency_issues: Counter[str] = Counter()
    all_identity_perspective: Counter[str] = Counter()
    all_proposals: Counter[tuple[str, str]] = Counter()
    slowest_turns: list[dict[str, Any]] = []
    job_failure_entries: list[dict[str, Any]] = []
    llm_calls = 0
    llm_elapsed: list[int] = []
    llm_providers: Counter[str] = Counter()
    llm_models: Counter[str] = Counter()

    for cycle, evaluation in zip(cycle_runs_payload, evaluations, strict=False):
        all_failures.update(str(item) for item in evaluation.failures)
        for item in evaluation.surprises:
            surprise = asdict(item) if hasattr(item, "__dataclass_fields__") else (dict(item) if isinstance(item, dict) else {"observed_behavior": str(item)})
            label = f"{surprise.get('scenario')}: {surprise.get('observed_behavior')}"
            all_surprises[label] += 1
        for warning in evaluation.warnings:
            lowered = warning.lower()
            if "elapsed" in lowered or "latency" in lowered:
                all_latency_issues[warning] += 1
        for failure in evaluation.failures:
            lowered = failure.lower()
            if "role inversion" in lowered or "identity contamination" in lowered:
                all_identity_perspective[failure] += 1
        for item in cycle.get("proposed_improvements") or []:
            key = (str(item.get("summary") or ""), str(item.get("affected_subsystem") or ""))
            all_proposals[key] += 1
        for scenario_run in cycle.get("scenario_runs") or []:
            scenario_name = scenario_run.get("scenario")
            for turn in scenario_run.get("turns") or []:
                if str(turn.get("kind") or "") != "user":
                    continue
                llm_calls += len(turn.get("llm_calls") or [])
                for call in turn.get("llm_calls") or []:
                    llm_elapsed.append(int(call.get("elapsed_ms") or 0))
                    llm_providers[str(call.get("provider") or "")] += 1
                    llm_models[str(call.get("model") or "")] += 1
                if turn.get("failed_jobs"):
                    job_failure_entries.append(
                        {
                            "run_id": cycle.get("run_id"),
                            "scenario": scenario_name,
                            "turn_number": turn.get("index"),
                            "failures": turn.get("failed_jobs"),
                        }
                    )
                slowest_turns.append(
                    {
                        "run_id": cycle.get("run_id"),
                        "scenario": scenario_name,
                        "turn_number": turn.get("index"),
                        "user_input": turn.get("user_input"),
                        "elapsed_ms": int(turn.get("elapsed_ms") or 0),
                        "fast_path_used": bool(turn.get("fast_path_used")),
                        "llm_calls": len(turn.get("llm_calls") or []),
                    }
                )

    slowest_turns.sort(key=lambda item: item["elapsed_ms"], reverse=True)
    proposal_rows = []
    seen_proposals: set[tuple[str, str]] = set()
    for (summary, subsystem), count in all_proposals.most_common():
        if (summary, subsystem) in seen_proposals:
            continue
        seen_proposals.add((summary, subsystem))
        proposal_rows.append({"summary": summary, "affected_subsystem": subsystem, "count": count})

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "completed",
        "failure_classification": None,
        "cycle_count": len(cycle_runs_payload),
        "run_ids": [cycle.get("run_id") for cycle in cycle_runs_payload],
        "scenarios": sorted({scenario for cycle in cycle_runs_payload for scenario in cycle.get("scenarios") or []}),
        "cycle_runs": cycle_runs_payload,
        "passed": all(cycle.get("passed") is True for cycle in cycle_runs_payload),
        "cleanup_status": (
            "cycles-wiped"
            if cycle_runs_payload and all(str(cycle.get("cleanup_status") or "") == "wiped" for cycle in cycle_runs_payload)
            else "kept"
        ),
        "totals": {
            "pass_count": sum(int(cycle.get("pass_count") or 0) for cycle in cycle_runs_payload),
            "fail_count": sum(1 for cycle in cycle_runs_payload for scenario_run in cycle.get("scenario_runs") or [] if not scenario_run.get("passed")),
            "warning_count": sum(len(scenario_run.get("warnings") or []) for cycle in cycle_runs_payload for scenario_run in cycle.get("scenario_runs") or []),
        },
        "recurring_failures": _counter_to_list(all_failures),
        "recurring_surprises": _counter_to_list(all_surprises),
        "recurring_latency_issues": _counter_to_list(all_latency_issues),
        "recurring_identity_perspective_problems": _counter_to_list(all_identity_perspective),
        "top_proposed_changes": proposal_rows,
        "slowest_turns": slowest_turns[:20],
        "llm_call_summary": {
            "call_count": llm_calls,
            "providers": dict(llm_providers),
            "models": dict(llm_models),
            "elapsed_ms": llm_elapsed,
        },
        "job_failure_summary": {
            "count": len(job_failure_entries),
            "entries": job_failure_entries,
        },
        "cleanup_status_by_cycle": [
            {
                "run_id": cycle.get("run_id"),
                "cleanup_status": cycle.get("cleanup_status"),
                "wiped": cycle.get("wiped"),
            }
            for cycle in cycle_runs_payload
        ],
    }
    if meta:
        payload.update({key: value for key, value in meta.items() if key not in {"cycle_runs", "run_ids", "scenarios", "cycle_count"}})
    return payload


def _cycle_summary_from_result(run_result: dict[str, Any], evaluation: RunEvaluation) -> dict[str, Any]:
    return {
        "run_id": run_result.get("run_id"),
        "cycle_index": run_result.get("cycle_index"),
        "seed": run_result.get("seed"),
        "created_at": run_result.get("created_at"),
        "scenarios": run_result.get("scenarios") or [],
        "passed": evaluation.passed,
        "pass_count": run_result.get("pass_count", 0),
        "fail_count": run_result.get("fail_count", 0),
        "warning_count": run_result.get("warning_count", 0),
        "cleanup_status": run_result.get("cleanup_status"),
        "wiped": run_result.get("wiped", False),
        "proposed_improvements": run_result.get("proposed_improvements") or [],
        "scenario_runs": run_result.get("scenario_runs") or [],
        "turns": [turn for scenario_run in run_result.get("scenario_runs") or [] for turn in scenario_run.get("turns") or []],
    }


def _counter_to_list(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"item": item, "count": count} for item, count in counter.most_common()]


def _append_bullet_list(lines: list[str], items: list[Any]) -> None:
    if not items:
        lines.append("- none")
        return
    for item in items:
        if isinstance(item, dict):
            if "summary" in item and "affected_subsystem" in item:
                lines.append(f"- {item['summary']} ({item['affected_subsystem']})")
            elif "item" in item and "count" in item:
                lines.append(f"- {item['item']} ({item['count']})")
            elif "run_id" in item and "cleanup_status" in item:
                lines.append(f"- {item['run_id']}: {item['cleanup_status']}")
            else:
                lines.append(f"- {json.dumps(item, ensure_ascii=True)}")
        else:
            lines.append(f"- {item}")
