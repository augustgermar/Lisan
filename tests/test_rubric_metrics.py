"""WO-3: rubric generation is deterministic from the kernel; metrics read
existing state and default to zero for unbuilt organs; the judge harness
parses and clamps model output defensively."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_EVALS = Path(__file__).resolve().parents[1] / "evals"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _EVALS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rubric_mod = _load("rubric")
metrics_mod = _load("metrics")
judge_mod = _load("judge")


def _seed_kernel_with_voice(vault: Path) -> None:
    from lisan.tools.kernel import ceremony, stamp_kernel_hash

    path = vault / "primer" / "identity-core.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with ceremony():
        path.write_text(
            "---\nassistant:\n  name: \"Vega\"\n---\n\n# Identity Core\n\n"
            "## Voice\n\n- Speaks tersely and dryly.\n- Never uses exclamation points.\n\n"
            "## Voice Provenance\n\n- ratified: 2026-07-04 by owner\n",
            encoding="utf-8",
        )
        stamp_kernel_hash(vault)


def test_rubric_is_deterministic_and_kernel_derived(tmp_path):
    _seed_kernel_with_voice(tmp_path)
    r1 = rubric_mod.rubric_from_kernel(tmp_path)
    r2 = rubric_mod.rubric_from_kernel(tmp_path)
    assert r1 == r2
    voice_dims = [d for d in r1["dimensions"] if d["kind"] == "voice"]
    assert len(voice_dims) == 2
    assert any("exclamation" in d["statement"] for d in voice_dims)
    global_ids = {d["id"] for d in r1["dimensions"] if d["kind"] == "global"}
    assert global_ids == {"continuity", "initiative", "self-consistency", "non-confabulation"}
    assert r1["generated_from_kernel_hash"] not in ("", "unstamped")


def test_rubric_without_kernel_voice_has_only_globals(tmp_path):
    rubric = rubric_mod.rubric_from_kernel(tmp_path)
    assert all(d["kind"] == "global" for d in rubric["dimensions"])


def test_metrics_default_to_zero_on_fresh_vault(tmp_path):
    metrics = metrics_mod.compute_metrics(tmp_path)
    assert metrics["open_loops_total"] == 0
    assert metrics["callbacks_delivered"] == 0
    assert metrics["self_belief_revisions"] == 0


def test_open_loop_closure_rate(tmp_path):
    from lisan.frontmatter import write_markdown

    loops = tmp_path / "open_loops"
    for i, status in enumerate(["open", "resolved", "open", "resolved"]):
        write_markdown(loops / f"loop-{i}.md", {"type": "open_loop", "status": status}, "body")
    metrics = metrics_mod.compute_metrics(tmp_path)
    assert metrics["open_loops_total"] == 4
    assert metrics["closure_rate"] == 0.5


def test_callback_marker_counting(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "lisan.log").write_text(
        "x drive.callback.delivered loop=a\ny drive.callback.suppressed loop=b cooldown\n"
        "z drive.callback.delivered loop=c\n",
        encoding="utf-8",
    )
    metrics = metrics_mod.compute_metrics(tmp_path)
    assert metrics["callbacks_delivered"] == 2
    assert metrics["callbacks_suppressed"] == 1


class _FakeLLM:
    def __init__(self, text):
        self.text = text
        self.last_prompt = None

    def complete(self, prompt, **kwargs):
        self.last_prompt = prompt

        class R:
            pass

        r = R()
        r.text = self.text
        return r


def test_judge_parses_and_clamps(tmp_path):
    _seed_kernel_with_voice(tmp_path)
    rubric = rubric_mod.rubric_from_kernel(tmp_path)
    dim = rubric["dimensions"][0]["id"]
    fake = _FakeLLM(json.dumps({"scores": [
        {"id": dim, "score": 9, "rationale": "clamped"},
        {"id": "not-a-dimension", "score": 3},
        {"id": "continuity", "score": None, "rationale": "no evidence"},
    ]}))
    scores = judge_mod.judge_exchange(rubric, "hi", "hello", llm=fake)
    assert {s["id"] for s in scores} == {dim, "continuity"}
    assert next(s for s in scores if s["id"] == dim)["score"] == 5
    assert next(s for s in scores if s["id"] == "continuity")["score"] is None
    assert "DIMENSIONS" in fake.last_prompt


def test_judge_context_enters_prompt(tmp_path):
    _seed_kernel_with_voice(tmp_path)
    rubric = rubric_mod.rubric_from_kernel(tmp_path)
    fake = _FakeLLM(json.dumps({"scores": []}))
    judge_mod.judge_exchange(rubric, "tell me everything", "recap", llm=fake,
                             context="USER: the barn find\nASSISTANT: noted")
    assert "CONVERSATION_CONTEXT" in fake.last_prompt
    assert "the barn find" in fake.last_prompt
    fake2 = _FakeLLM(json.dumps({"scores": []}))
    judge_mod.judge_exchange(rubric, "hi", "hello", llm=fake2)
    assert "CONVERSATION_CONTEXT" not in fake2.last_prompt


def test_aggregate_ignores_nulls():
    agg = judge_mod.aggregate([
        [{"id": "a", "score": 4}, {"id": "b", "score": None}],
        [{"id": "a", "score": 5}, {"id": "b", "score": 2}],
    ])
    assert agg["a"] == {"mean": 4.5, "n": 2}
    assert agg["b"] == {"mean": 2.0, "n": 1}


def test_probe_set_is_well_formed():
    spec = json.loads((_EVALS / "probes" / "baseline_v1.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in spec["probes"]]
    assert len(ids) == len(set(ids))
    assert all(p["text"].strip() for p in spec["probes"])
    banned = ("august", "maya", "josie", "pip", "varga", "chico", "vega")
    blob = json.dumps(spec).lower()
    assert not any(name in blob for name in banned)
