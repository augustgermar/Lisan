from evals.memory_e2e import run_deterministic_e2e


def test_disposable_raw_capture_through_answer_harness():
    result = run_deterministic_e2e(budget_chars=8000)
    assert result.passed, result
    assert all(result.stages.values())
    assert result.model_dependent == "unrun"
    assert result.metrics["included_chars"] <= 8000
    assert result.provenance["embedding_model"] == "deterministic-fake-provider"
