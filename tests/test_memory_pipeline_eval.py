from lisan.tools.self_eval import run_memory_pipeline_evaluation
from lisan.cli import build_parser


def test_release_gate_has_a_cli_entrypoint():
    assert build_parser().parse_args(["self", "eval-memory"]).self_command == "eval-memory"


def test_memory_pipeline_release_gate():
    result = run_memory_pipeline_evaluation()
    # A known gap is not a release exemption: this gate should fail until the
    # historical selection behavior is actually corrected.
    failed = [case for case in result["cases"] if not case["passed"]]
    assert not failed, failed
    assert result["passed"] >= 5
    assert result["known_gaps"] == 0
