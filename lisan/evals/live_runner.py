from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import load_config
from ..paths import ensure_repo_layout, repo_root
from ..tools.chat import _process_chat_turn
from ..tools.jobs import run_jobs_worker
from ..tools.narrative_state import reset_narrative_state
from ..tools.provider_diagnostics import diagnose_provider
from .live_oracle import (
    UnexpectedBehavior,
    RunEvaluation,
    classify_record_path,
    evaluate_run,
    record_payload,
    to_jsonable_issue,
)
from .live_report import write_aggregate_report, write_run_report
from .live_scenarios import ScenarioDefinition, ScenarioStep, TurnExpectation, get_scenario, list_scenarios
from .live_wipe import wipe_live_run


TurnHandler = Callable[..., dict[str, Any]]


@dataclass(slots=True)
class TurnObservation:
    index: int
    kind: str
    label: str
    user_input: str
    assistant_output: str
    elapsed_ms: int
    trace_id: str | None
    classification: dict[str, Any]
    fast_path_used: bool
    deterministic_response_used: bool
    retrieval_used: bool
    retrieval_record_count: int
    graph_expanded_count: int
    jobs_queued: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    durable_records: list[dict[str, Any]] = field(default_factory=list)
    transient_records: list[dict[str, Any]] = field(default_factory=list)
    jobs_worker_summary: dict[str, Any] | None = None
    completed_jobs: list[dict[str, Any]] = field(default_factory=list)
    failed_jobs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "label": self.label,
            "user_input": self.user_input,
            "assistant_output": self.assistant_output,
            "elapsed_ms": self.elapsed_ms,
            "trace_id": self.trace_id,
            "classification": self.classification,
            "fast_path_used": self.fast_path_used,
            "deterministic_response_used": self.deterministic_response_used,
            "retrieval_used": self.retrieval_used,
            "retrieval_record_count": self.retrieval_record_count,
            "graph_expanded_count": self.graph_expanded_count,
            "jobs_queued": self.jobs_queued,
            "llm_calls": self.llm_calls,
            "warnings": self.warnings,
            "trace": self.trace,
            "durable_records": self.durable_records,
            "transient_records": self.transient_records,
            "jobs_worker_summary": self.jobs_worker_summary,
            "completed_jobs": self.completed_jobs,
            "failed_jobs": self.failed_jobs,
        }


@dataclass(slots=True)
class ScenarioRunObservation:
    scenario: str
    conversation_id: str
    status: str = "passed"
    provider_failure: bool = False
    provider_error: str | None = None
    turns: list[TurnObservation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    surprises: list[UnexpectedBehavior] = field(default_factory=list)
    durable_records: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True
    durable_records_total: int = 0
    jobs_run_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "provider_failure": self.provider_failure,
            "provider_error": self.provider_error,
            "turns": [turn.to_dict() for turn in self.turns],
            "warnings": self.warnings,
            "failures": self.failures,
            "positives": self.positives,
            "surprises": [to_jsonable_issue(item) for item in self.surprises],
            "durable_records": self.durable_records,
            "passed": self.passed,
            "durable_records_total": self.durable_records_total,
            "jobs_run_total": self.jobs_run_total,
        }


@dataclass(slots=True)
class LiveEvalCycleObservation:
    run_id: str
    cycle_index: int
    seed: int | None
    created_at: str
    run_root: Path
    vault: Path
    db_path: Path
    scenarios: list[str]
    scenario_runs: list[ScenarioRunObservation] = field(default_factory=list)
    positive_surprises: list[dict[str, Any]] = field(default_factory=list)
    negative_surprises: list[dict[str, Any]] = field(default_factory=list)
    proposed_improvements: list[dict[str, Any]] = field(default_factory=list)
    pass_count: int = 0
    fail_count: int = 0
    warning_count: int = 0
    cleanup_status: str = "not-run"
    latency_summary: dict[str, Any] = field(default_factory=dict)
    llm_summary: dict[str, Any] = field(default_factory=dict)
    job_summary: dict[str, Any] = field(default_factory=dict)
    retrieval_summary: dict[str, Any] = field(default_factory=dict)
    durable_records: list[dict[str, Any]] = field(default_factory=list)
    transcript_md: str = ""
    transcript_json: list[dict[str, Any]] = field(default_factory=list)
    provider_info: dict[str, Any] = field(default_factory=dict)
    provider_auth_mode: str = "shared"
    report_paths: dict[str, Path] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    wiped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cycle_index": self.cycle_index,
            "seed": self.seed,
            "created_at": self.created_at,
            "run_root": str(self.run_root),
            "vault": str(self.vault),
            "db_path": str(self.db_path),
            "scenarios": self.scenarios,
            "scenario_runs": [scenario_run.to_dict() for scenario_run in self.scenario_runs],
            "positive_surprises": self.positive_surprises,
            "negative_surprises": self.negative_surprises,
            "proposed_improvements": self.proposed_improvements,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "warning_count": self.warning_count,
            "cleanup_status": self.cleanup_status,
            "latency_summary": self.latency_summary,
            "llm_summary": self.llm_summary,
            "job_summary": self.job_summary,
            "retrieval_summary": self.retrieval_summary,
            "durable_records": self.durable_records,
            "transcript_md": self.transcript_md,
            "transcript_json": self.transcript_json,
            "provider_info": self.provider_info,
            "provider_auth_mode": self.provider_auth_mode,
            "report_paths": {key: str(value) for key, value in self.report_paths.items()},
            "evaluation": self.evaluation,
            "wiped": self.wiped,
        }


@dataclass(slots=True)
class LiveEvalAggregateObservation:
    aggregate_id: str
    created_at: str
    aggregate_root: Path
    scenarios: list[str]
    cycle_count: int
    status: str = "completed"
    failure_classification: str | None = None
    provider_diagnostics: dict[str, Any] = field(default_factory=dict)
    provider_auth_mode: str = "shared"
    cycle_runs: list[LiveEvalCycleObservation] = field(default_factory=list)
    report_paths: dict[str, Path] = field(default_factory=dict)
    pass_count: int = 0
    fail_count: int = 0
    provider_failure_count: int = 0
    warning_count: int = 0
    cleanup_status: str = "not-run"
    latency_summary: dict[str, Any] = field(default_factory=dict)
    llm_summary: dict[str, Any] = field(default_factory=dict)
    job_summary: dict[str, Any] = field(default_factory=dict)
    retrieval_summary: dict[str, Any] = field(default_factory=dict)
    positive_surprises: list[dict[str, Any]] = field(default_factory=list)
    negative_surprises: list[dict[str, Any]] = field(default_factory=list)
    proposed_improvements: list[dict[str, Any]] = field(default_factory=list)
    evaluation: dict[str, Any] = field(default_factory=dict)
    transcript_md: str = ""
    transcript_json: list[dict[str, Any]] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.aggregate_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.aggregate_id,
            "aggregate_id": self.aggregate_id,
            "created_at": self.created_at,
            "aggregate_root": str(self.aggregate_root),
            "scenarios": self.scenarios,
            "cycle_count": self.cycle_count,
            "status": self.status,
            "failure_classification": self.failure_classification,
            "provider_diagnostics": self.provider_diagnostics,
            "provider_auth_mode": self.provider_auth_mode,
            "cycle_runs": [cycle_run.to_dict() for cycle_run in self.cycle_runs],
            "report_paths": {key: str(value) for key, value in self.report_paths.items()},
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "provider_failure_count": self.provider_failure_count,
            "warning_count": self.warning_count,
            "cleanup_status": self.cleanup_status,
            "latency_summary": self.latency_summary,
            "llm_summary": self.llm_summary,
            "job_summary": self.job_summary,
            "retrieval_summary": self.retrieval_summary,
            "positive_surprises": self.positive_surprises,
            "negative_surprises": self.negative_surprises,
            "proposed_improvements": self.proposed_improvements,
            "evaluation": self.evaluation,
            "transcript_md": self.transcript_md,
            "transcript_json": self.transcript_json,
        }


def run_live_eval(
    *,
    scenarios: list[str] | None = None,
    cycles: int = 1,
    seed: int | None = None,
    wipe_after: bool = False,
    run_jobs_after_turn: bool = False,
    run_jobs_at_end: bool = True,
    no_jobs: bool = False,
    dangerous_vault: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    provider_auth: str = "shared",
    turn_handler: TurnHandler | None = None,
) -> LiveEvalAggregateObservation:
    ensure_repo_layout()
    root = repo_root() / ".lisan_live_eval_runs"
    root.mkdir(parents=True, exist_ok=True)
    selected_scenarios = scenarios or list_scenarios()
    turn_handler = turn_handler or _default_turn_handler
    config = load_config()
    auth_mode = _normalize_provider_auth(provider_auth)
    effective_provider = "mock" if auth_mode == "mock" else provider
    effective_model = model if auth_mode != "mock" else (model or "mock")

    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    session_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_token = secrets.token_hex(2)
    aggregate_id = f"aggregate_{session_stamp}_{session_token}"
    aggregate_root = root / aggregate_id
    aggregate_root.mkdir(parents=True, exist_ok=False)

    provider_name = _resolved_live_provider(config, effective_provider)
    provider_diag_home = _provider_diagnostic_home(provider_name, auth_mode, aggregate_root)
    if auth_mode == "mock":
        provider_diagnostics = _mock_provider_diagnostics(provider_name, effective_model, auth_mode)
    else:
        provider_diagnostics = diagnose_provider(
            provider=effective_provider,
            model=effective_model,
            config=config,
            session_home=provider_diag_home,
        )

    aggregate = LiveEvalAggregateObservation(
        aggregate_id=aggregate_id,
        created_at=created_at,
        aggregate_root=aggregate_root,
        scenarios=selected_scenarios,
        cycle_count=cycles,
        provider_diagnostics=provider_diagnostics.to_dict(),
        provider_auth_mode=auth_mode,
    )

    if provider_diagnostics.status == "failed":
        aggregate.status = "infrastructure_failed"
        aggregate.failure_classification = provider_diagnostics.error_type or "provider_failure"
        aggregate.cleanup_status = "kept"
        aggregate.report_paths = write_aggregate_report(
            [],
            [],
            aggregate_root,
            [],
            meta=aggregate.to_dict(),
        )
        return aggregate

    cycle_seeds = _build_cycle_seeds(cycles, seed)
    cycle_runs: list[LiveEvalCycleObservation] = []
    for cycle_index in range(cycles):
        cycle_root = root / f"live_eval_{session_stamp}_{session_token}_cycle_{cycle_index + 1:03d}"
        provider_home = _provider_cycle_home(provider_name, auth_mode, cycle_root)
        with _temporary_env({"LISAN_CODEX_HOME": str(provider_home)} if provider_home is not None else {}):
            cycle_run = _run_cycle(
                cycle_index=cycle_index,
                cycle_seed=cycle_seeds[cycle_index],
                session_stamp=session_stamp,
                session_token=session_token,
                selected_scenarios=selected_scenarios,
                root=root,
                config=config,
                dangerous_vault=dangerous_vault,
                provider=effective_provider,
                model=effective_model,
                turn_handler=turn_handler,
                run_jobs_after_turn=run_jobs_after_turn,
                run_jobs_at_end=run_jobs_at_end,
                no_jobs=no_jobs,
                provider_auth=auth_mode,
            )
        cycle_runs.append(cycle_run)

    aggregate.cycle_runs = cycle_runs
    aggregate.pass_count = sum(1 for cycle in cycle_runs for scenario_run in cycle.scenario_runs if scenario_run.status == "passed")
    aggregate.fail_count = sum(1 for cycle in cycle_runs for scenario_run in cycle.scenario_runs if scenario_run.status == "behavioral_failed")
    aggregate.provider_failure_count = sum(1 for cycle in cycle_runs for scenario_run in cycle.scenario_runs if scenario_run.status == "provider_failed")
    if aggregate.provider_failure_count > 0:
        aggregate.status = "provider_failed"
        aggregate.failure_classification = "provider_failure"
    elif aggregate.fail_count > 0:
        aggregate.status = "behavioral_failed"
        aggregate.failure_classification = "behavioral_failure"
    aggregate.warning_count = sum(len(scenario_run.warnings) for cycle in cycle_runs for scenario_run in cycle.scenario_runs)
    all_scenario_runs = [scenario_run for cycle in cycle_runs for scenario_run in cycle.scenario_runs]
    all_evaluations = {name: get_scenario(name) for name in selected_scenarios}
    evaluation = evaluate_run({"scenario_runs": [scenario_run.to_dict() for scenario_run in all_scenario_runs]}, all_evaluations)
    aggregate.evaluation = evaluation.to_dict()
    aggregate.negative_surprises = [to_jsonable_issue(item) for item in evaluation.surprises if item.polarity == "negative"]
    aggregate.positive_surprises = [to_jsonable_issue(item) for item in evaluation.surprises if item.polarity == "positive"]
    aggregate.proposed_improvements = _build_proposed_improvements(aggregate.negative_surprises)
    aggregate.transcript_md = _render_transcript(all_scenario_runs)
    aggregate.transcript_json = [scenario_run.to_dict() for scenario_run in all_scenario_runs]
    aggregate.latency_summary = _summarize_latency(all_scenario_runs)
    aggregate.llm_summary = _summarize_llm(all_scenario_runs)
    aggregate.job_summary = _summarize_jobs(all_scenario_runs)
    aggregate.retrieval_summary = _summarize_retrieval(all_scenario_runs)

    if wipe_after:
        for cycle_run in cycle_runs:
            if cycle_run.wiped:
                continue
            wipe_result = wipe_live_run(cycle_run.run_root)
            cycle_run.wiped = bool(wipe_result.get("wiped"))
            cycle_run.cleanup_status = "wiped" if cycle_run.wiped else str(wipe_result.get("reason") or "kept")
            if cycle_run.report_paths:
                report_json = cycle_run.report_paths.get("json")
                if report_json and report_json.exists():
                    payload = json.loads(report_json.read_text(encoding="utf-8"))
                    payload["wiped"] = cycle_run.wiped
                    payload["cleanup_status"] = cycle_run.cleanup_status
                    report_json.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

        aggregate.cleanup_status = "cycles-wiped"
    else:
        aggregate.cleanup_status = "kept"

    aggregate_evaluations: list[RunEvaluation] = []
    for cycle in cycle_runs:
        if any(scenario_run.status in {"passed", "behavioral_failed"} for scenario_run in cycle.scenario_runs):
            aggregate_evaluations.append(
                evaluate_run(
                    {"scenario_runs": [scenario_run.to_dict() for scenario_run in cycle.scenario_runs if scenario_run.status in {"passed", "behavioral_failed"}]},
                    {name: get_scenario(name) for name in selected_scenarios},
                )
            )
        else:
            aggregate_evaluations.append(RunEvaluation(passed=False))
    aggregate.report_paths = write_aggregate_report(
        [cycle.to_dict() for cycle in cycle_runs],
        aggregate_evaluations,
        aggregate_root,
        cycle_runs,
        meta=aggregate.to_dict(),
    )

    return aggregate


def _default_turn_handler(**kwargs: Any) -> dict[str, Any]:
    return _process_chat_turn(**kwargs)


def _prepare_run_dirs(run_root: Path, vault: Path) -> None:
    for rel in ["state", "logs", "reports", "traces"]:
        (run_root / rel).mkdir(parents=True, exist_ok=True)
    if vault != run_root / "vault":
        vault.mkdir(parents=True, exist_ok=True)
    else:
        vault.mkdir(parents=True, exist_ok=True)
    ensure_repo_layout(vault)
    (vault / "logs" / "traces").mkdir(parents=True, exist_ok=True)


def _write_marker(run_root: Path) -> None:
    (run_root / ".lisan_eval_vault").write_text("live eval vault\n", encoding="utf-8")


def _write_run_manifest(run_root: Path, scenarios: list[str], cycles: int, provider: str | None, model: str | None, provider_auth: str, config: dict[str, Any], dangerous_vault: Path | None) -> None:
    manifest = {
        "scenarios": scenarios,
        "cycles": cycles,
        "provider": provider,
        "model": model,
        "provider_auth": provider_auth,
        "dangerous_vault": str(dangerous_vault) if dangerous_vault is not None else None,
        "config": {
            "local_base_url": os.environ.get("LISAN_LOCAL_MODEL_URL", config.get("providers", {}).get("local", {}).get("base_url")),
            "local_model": os.environ.get("LISAN_LOCAL_MODEL", config.get("providers", {}).get("local", {}).get("default_model")),
        },
    }
    (run_root / "run_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _provider_info(config: dict[str, Any], provider: str | None, model: str | None, provider_auth: str) -> dict[str, Any]:
    local = config.get("providers", {}).get("local", {})
    return {
        "provider": provider or "local",
        "model": model or os.environ.get("LISAN_LOCAL_MODEL", local.get("default_model", "")),
        "base_url": os.environ.get("LISAN_LOCAL_MODEL_URL", local.get("base_url", "")),
        "provider_auth": provider_auth,
    }


@contextmanager
def _temporary_env(overrides: dict[str, str]) -> Any:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_cycle_seeds(cycles: int, seed: int | None) -> list[int | None]:
    if cycles <= 0:
        return []
    if seed is None:
        return [secrets.randbits(31) for _ in range(cycles)]
    base_seed = int(seed)
    return [base_seed + offset for offset in range(cycles)]


def _normalize_provider_auth(provider_auth: str) -> str:
    value = str(provider_auth or "shared").strip().lower()
    if value not in {"shared", "isolated", "mock"}:
        return "shared"
    return value


def _resolved_live_provider(config: dict[str, Any], provider: str | None) -> str:
    if provider:
        return provider
    return str(config.get("routing", {}).get("elicitor", {}).get("medium", "codex") or "codex")


def _provider_diagnostic_home(provider_name: str, auth_mode: str, aggregate_root: Path) -> Path | None:
    if provider_name != "codex":
        return None
    if auth_mode == "isolated":
        return aggregate_root / "state" / "codex_home"
    return None


def _provider_cycle_home(provider_name: str, auth_mode: str, cycle_root: Path) -> Path | None:
    if provider_name != "codex":
        return None
    if auth_mode == "isolated":
        return cycle_root / "state" / "codex_home"
    return None


def _mock_provider_diagnostics(provider_name: str, model: str | None, auth_mode: str):
    from ..tools.provider_diagnostics import ProviderDiagnosticResult

    return ProviderDiagnosticResult(
        provider=provider_name or "mock",
        model=model or "mock",
        status="ok",
        error_type=None,
        binary="mock",
        binary_path="",
        session_home="",
        session_path="",
        session_writable=True,
        minimal_completion=True,
        elapsed_ms=0,
        errors=[],
        suggested_fixes=[],
        details={"mode": "mock", "provider_auth": auth_mode},
    )


def _run_cycle(
    *,
    cycle_index: int,
    cycle_seed: int | None,
    session_stamp: str,
    session_token: str,
    selected_scenarios: list[str],
    root: Path,
    config: dict[str, Any],
    dangerous_vault: Path | None,
    provider: str | None,
    model: str | None,
    provider_auth: str,
    turn_handler: TurnHandler,
    run_jobs_after_turn: bool,
    run_jobs_at_end: bool,
    no_jobs: bool,
) -> LiveEvalCycleObservation:
    run_id = f"live_eval_{session_stamp}_{session_token}_cycle_{cycle_index + 1:03d}"
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    run_root = root / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    vault = dangerous_vault if dangerous_vault is not None else run_root / "vault"
    db_path = run_root / "state" / "lisan.sqlite"
    _prepare_run_dirs(run_root, vault)
    _write_marker(run_root)
    _write_cycle_manifest(
        run_root,
        scenarios=selected_scenarios,
        cycle_index=cycle_index,
        cycle_seed=cycle_seed,
        session_stamp=session_stamp,
        session_token=session_token,
        provider=provider,
        model=model,
        provider_auth=provider_auth,
        config=config,
        dangerous_vault=dangerous_vault,
    )

    cycle_obs = LiveEvalCycleObservation(
        run_id=run_id,
        cycle_index=cycle_index + 1,
        seed=cycle_seed,
        created_at=created_at,
        run_root=run_root,
        vault=vault,
        db_path=db_path,
        scenarios=selected_scenarios,
        provider_info=_provider_info(config, provider, model, provider_auth),
        provider_auth_mode=provider_auth,
    )

    run_records: list[dict[str, Any]] = []
    for scenario_name in selected_scenarios:
        scenario = get_scenario(scenario_name)
        scenario_run = _run_scenario(
            scenario=scenario,
            cycle_label=run_id,
            vault=vault,
            db_path=db_path,
            run_root=run_root,
            turn_handler=turn_handler,
            provider=provider,
            model=model,
            run_jobs_after_turn=run_jobs_after_turn,
            run_jobs_at_end=run_jobs_at_end,
            no_jobs=no_jobs,
        )
        cycle_obs.scenario_runs.append(scenario_run)
        run_records.append(scenario_run.to_dict())

    evaluation = evaluate_run({"scenario_runs": run_records}, {name: get_scenario(name) for name in selected_scenarios})
    cycle_obs.evaluation = evaluation.to_dict()
    cycle_obs.pass_count = sum(1 for scenario_run in cycle_obs.scenario_runs if scenario_run.passed)
    cycle_obs.fail_count = len(cycle_obs.scenario_runs) - cycle_obs.pass_count
    cycle_obs.warning_count = sum(len(scenario_run.warnings) for scenario_run in cycle_obs.scenario_runs)
    cycle_obs.negative_surprises = [to_jsonable_issue(item) for item in evaluation.surprises if item.polarity == "negative"]
    cycle_obs.positive_surprises = [to_jsonable_issue(item) for item in evaluation.surprises if item.polarity == "positive"]
    cycle_obs.proposed_improvements = _build_proposed_improvements(cycle_obs.negative_surprises)
    cycle_obs.transcript_md = _render_transcript(cycle_obs.scenario_runs)
    cycle_obs.transcript_json = [scenario_run.to_dict() for scenario_run in cycle_obs.scenario_runs]
    (run_root / "transcript.md").write_text(cycle_obs.transcript_md, encoding="utf-8")
    (run_root / "transcript.json").write_text(json.dumps(cycle_obs.transcript_json, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    cycle_obs.durable_records = _collect_durable_records(vault)
    cycle_obs.latency_summary = _summarize_latency(cycle_obs.scenario_runs)
    cycle_obs.llm_summary = _summarize_llm(cycle_obs.scenario_runs)
    cycle_obs.job_summary = _summarize_jobs(cycle_obs.scenario_runs)
    cycle_obs.retrieval_summary = _summarize_retrieval(cycle_obs.scenario_runs)
    cycle_obs.cleanup_status = "kept"
    cycle_obs.report_paths = write_run_report(cycle_obs.to_dict(), evaluation, run_root / "reports")
    return cycle_obs


def _run_scenario(
    *,
    scenario: ScenarioDefinition,
    cycle_label: str,
    vault: Path,
    db_path: Path,
    run_root: Path,
    turn_handler: TurnHandler,
    provider: str | None,
    model: str | None,
    run_jobs_after_turn: bool,
    run_jobs_at_end: bool,
    no_jobs: bool,
) -> ScenarioRunObservation:
    conversation_id = f"{scenario.name}-{cycle_label}"
    run_obs = ScenarioRunObservation(scenario=scenario.name, conversation_id=conversation_id)
    advice_history: list[dict[str, str]] = []
    advice_context_active = False
    advice_topic: str | None = None
    before_snapshot = _snapshot_vault(vault)

    for idx, step in enumerate(scenario.steps, start=1):
        if step.kind == "reset":
            reset_narrative_state(vault, conversation_id)
            advice_history.clear()
            advice_context_active = False
            advice_topic = None
            run_obs.turns.append(
                TurnObservation(
                    index=idx,
                    kind="reset",
                    label=step.label or "reset",
                    user_input="",
                    assistant_output="",
                    elapsed_ms=0,
                    trace_id=None,
                    classification={"route": "reset"},
                    fast_path_used=True,
                    deterministic_response_used=True,
                    retrieval_used=False,
                    retrieval_record_count=0,
                    graph_expanded_count=0,
                    jobs_queued=[],
                    llm_calls=[],
                    trace={},
                )
            )
            continue

        turn_before = _snapshot_vault(vault)
        started = time.time()
        result = turn_handler(
            vault=vault,
            conversation_id=conversation_id,
            text=step.text,
            provider=provider,
            model=model,
            advice_history=advice_history,
            advice_context_active=advice_context_active,
            advice_topic=advice_topic,
            domain_override=None,
            db_path=db_path,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        trace = result.get("trace") or {}
        output = str(result.get("response") or "")
        trace_path = trace.get("trace_path")
        if trace_path and Path(trace_path).exists():
            shutil.copy2(Path(trace_path), run_root / "traces" / Path(trace_path).name)
        if step.run_jobs_after and not no_jobs:
            worker_summary = run_jobs_worker(vault=vault, db_path=db_path, provider=provider, model=model)
        elif run_jobs_after_turn and not no_jobs:
            worker_summary = run_jobs_worker(vault=vault, db_path=db_path, provider=provider, model=model)
        else:
            worker_summary = None
        after_snapshot = _snapshot_vault(vault)
        delta = _diff_snapshots(vault, turn_before, after_snapshot)
        durable_records = [record_payload(path) for path in delta["durable"] if path.suffix == ".md" and path.exists()]
        transient_records = [record_payload(path) for path in delta["transient"] if path.suffix == ".md" and path.exists()]
        if step.kind == "user":
            advice_context_active = result.get("route") == "advice"
            if advice_context_active:
                advice_topic = str(result.get("topic") or advice_topic or "")
                advice_history.append({"speaker": "user", "text": step.text})
                advice_history.append({"speaker": "assistant", "text": output})
            turn_obs = TurnObservation(
                index=idx,
                kind="user",
                label=step.label or "",
                user_input=step.text,
                assistant_output=output,
                elapsed_ms=elapsed_ms,
                trace_id=str(trace.get("turn_id") or ""),
                classification={"route": result.get("route"), "kind": result.get("kind"), "fast_path_used": result.get("fast_path_used")},
                fast_path_used=bool(result.get("fast_path_used")),
                deterministic_response_used=bool(result.get("fast_path_used") and trace.get("llm_calls") == []),
                retrieval_used=bool(trace.get("retrieval_used")),
                retrieval_record_count=int(trace.get("retrieval_record_count") or 0),
                graph_expanded_count=int(trace.get("graph_expanded_count") or 0),
                jobs_queued=list(result.get("queued_jobs") or []),
                llm_calls=list(trace.get("llm_calls") or []),
                warnings=[],
                trace=trace,
                durable_records=durable_records,
                transient_records=transient_records,
                jobs_worker_summary=worker_summary,
                completed_jobs=(worker_summary or {}).get("successes", []) if worker_summary else [],
                failed_jobs=(worker_summary or {}).get("failures", []) if worker_summary else [],
            )
            run_obs.turns.append(turn_obs)
            if worker_summary:
                run_obs.jobs_run_total += int(worker_summary.get("processed_count") or 0)
            if result.get("provider_failure"):
                run_obs.status = "provider_failed"
                run_obs.provider_failure = True
                run_obs.provider_error = str(result.get("error") or "")
                break
        if run_jobs_at_end and not no_jobs and idx == len(scenario.steps):
            worker_summary = run_jobs_worker(vault=vault, db_path=db_path, provider=provider, model=model)
            run_obs.jobs_run_total += int(worker_summary.get("processed_count") or 0)

    after_snapshot = _snapshot_vault(vault)
    _diff_snapshots(vault, before_snapshot, after_snapshot)
    run_obs.durable_records = _collect_durable_records(vault)
    run_obs.durable_records_total = len(run_obs.durable_records)
    if run_obs.status == "passed":
        evaluation = evaluate_run({"scenario_runs": [run_obs.to_dict()]}, {scenario.name: scenario})
        scenario_eval = evaluation.scenario_evaluations[0] if evaluation.scenario_evaluations else None
        if scenario_eval is not None:
            run_obs.passed = scenario_eval.passed
            run_obs.warnings = scenario_eval.warnings
            run_obs.failures = scenario_eval.failures
            run_obs.positives = scenario_eval.positives
            run_obs.surprises = scenario_eval.surprises
        if not run_obs.passed:
            run_obs.status = "behavioral_failed"
    else:
        run_obs.passed = False
    return run_obs


def _write_cycle_manifest(
    run_root: Path,
    *,
    scenarios: list[str],
    cycle_index: int,
    cycle_seed: int | None,
    session_stamp: str,
    session_token: str,
    provider: str | None,
    model: str | None,
    provider_auth: str,
    config: dict[str, Any],
    dangerous_vault: Path | None,
) -> None:
    manifest = {
        "scenarios": scenarios,
        "cycle_index": cycle_index + 1,
        "cycle_seed": cycle_seed,
        "session_stamp": session_stamp,
        "session_token": session_token,
        "provider": provider,
        "model": model,
        "provider_auth": provider_auth,
        "dangerous_vault": str(dangerous_vault) if dangerous_vault is not None else None,
        "config": {
            "local_base_url": os.environ.get("LISAN_LOCAL_MODEL_URL", config.get("providers", {}).get("local", {}).get("base_url")),
            "local_model": os.environ.get("LISAN_LOCAL_MODEL", config.get("providers", {}).get("local", {}).get("default_model")),
        },
    }
    (run_root / "run_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _snapshot_vault(vault: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    if not vault.exists():
        return out
    for path in vault.rglob("*"):
        if path.is_file():
            stat = path.stat()
            out[str(path.relative_to(vault))] = (stat.st_size, stat.st_mtime_ns)
    return out


def _diff_snapshots(vault: Path, before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> dict[str, list[Path]]:
    durable: list[Path] = []
    transient: list[Path] = []
    for rel in after.keys():
        if rel not in before or before.get(rel) != after.get(rel):
            rel_path = Path(rel)
            path = vault / rel_path
            if classify_record_path(rel_path) == "durable":
                durable.append(path)
            elif classify_record_path(rel_path) == "transient":
                transient.append(path)
    return {"durable": durable, "transient": transient}


def _collect_durable_records(vault: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in vault.rglob("*.md"):
        if classify_record_path(path.relative_to(vault)) != "durable":
            continue
        try:
            records.append(record_payload(path))
        except Exception:
            continue
    return records


def _render_transcript(scenario_runs: list[ScenarioRunObservation]) -> str:
    lines = ["# Live Eval Transcript", ""]
    for run in scenario_runs:
        lines.append(f"## {run.scenario} ({run.conversation_id})")
        for turn in run.turns:
            if turn.kind == "reset":
                lines.append("- reset conversation")
                continue
            lines.append(f"- User: {turn.user_input}")
            lines.append(f"  Assistant: {turn.assistant_output}")
            lines.append(f"  Trace: fast={turn.fast_path_used} llm={len(turn.llm_calls)} retrieval={turn.retrieval_used} jobs={len(turn.jobs_queued)} elapsed={turn.elapsed_ms}ms")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summarize_latency(scenario_runs: list[ScenarioRunObservation]) -> dict[str, Any]:
    turn_count = 0
    elapsed_values: list[int] = []
    for run in scenario_runs:
        for turn in run.turns:
            if turn.kind != "user":
                continue
            turn_count += 1
            elapsed_values.append(int(turn.elapsed_ms))
    if not elapsed_values:
        return {"turn_count": 0}
    return {
        "turn_count": turn_count,
        "max_elapsed_ms": max(elapsed_values),
        "avg_elapsed_ms": round(sum(elapsed_values) / len(elapsed_values), 2),
    }


def _summarize_llm(scenario_runs: list[ScenarioRunObservation]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    for run in scenario_runs:
        for turn in run.turns:
            calls.extend(turn.llm_calls)
    if not calls:
        return {"call_count": 0}
    return {
        "call_count": len(calls),
        "providers": sorted({str(call.get("provider") or "") for call in calls}),
        "models": sorted({str(call.get("model") or "") for call in calls}),
        "elapsed_ms": [int(call.get("elapsed_ms") or 0) for call in calls],
    }


def _summarize_jobs(scenario_runs: list[ScenarioRunObservation]) -> dict[str, Any]:
    queued = 0
    processed = 0
    failed = 0
    for run in scenario_runs:
        for turn in run.turns:
            queued += len(turn.jobs_queued)
            if turn.jobs_worker_summary:
                processed += int(turn.jobs_worker_summary.get("processed_count") or 0)
                failed += int(turn.jobs_worker_summary.get("failure_count") or 0)
    return {
        "queued": queued,
        "processed": processed,
        "failed": failed,
    }


def _summarize_retrieval(scenario_runs: list[ScenarioRunObservation]) -> dict[str, Any]:
    turns = [turn for run in scenario_runs for turn in run.turns if turn.kind == "user"]
    if not turns:
        return {"turns": 0}
    retrieval_turns = sum(1 for turn in turns if turn.retrieval_used)
    return {
        "turns": len(turns),
        "retrieval_turns": retrieval_turns,
        "retrieval_record_count_total": sum(int(turn.retrieval_record_count) for turn in turns),
    }


def _build_proposed_improvements(negative_surprises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in negative_surprises:
        cause = str(item.get("likely_cause") or "unknown")
        if cause == "perspective_tracking_issue":
            summary = "Strengthen family-role extraction prompt to preserve parent/child directionality."
            subsystem = "memory_extraction"
            priority = "high"
        elif cause == "prompt_identity_issue":
            summary = "Tighten assistant identity anchoring so the assistant never inherits a user or family member identity."
            subsystem = "prompt_identity"
            priority = "high"
        elif cause == "fast_path_classifier_issue":
            summary = "Refine the fast-path classifier for this conversational shape."
            subsystem = "fast_path_classifier"
            priority = "medium"
        elif cause == "safety_policy_issue":
            summary = "Add a stronger safe-refusal template for manipulative or abusive requests."
            subsystem = "safety_policy"
            priority = "high"
        elif cause == "memory_extraction_issue":
            summary = "Queue or strengthen extraction for simple biographical facts and family relations."
            subsystem = "memory_extraction"
            priority = "high"
        else:
            summary = "Review the affected turn and add a focused regression test."
            subsystem = cause
            priority = "medium"
        key = (summary, subsystem)
        if key in seen:
            continue
        seen.add(key)
        proposals.append(
            {
                "summary": summary,
                "affected_subsystem": subsystem,
                "risk_level": item.get("severity") or "medium",
                "priority": priority,
                "evidence": {
                    "scenario": item.get("scenario"),
                    "turn_number": item.get("turn_number"),
                    "user_input": item.get("user_input"),
                    "assistant_output": item.get("assistant_output"),
                    "expected_behavior": item.get("expected_behavior"),
                    "observed_behavior": item.get("observed_behavior"),
                },
            }
        )
    return proposals


def _make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{secrets.token_hex(3)}"
