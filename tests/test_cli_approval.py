from __future__ import annotations

from lisan.cli import build_parser, main
from lisan.tools.chat import _cli_approval_fn
from lisan.tools import execution_tools


def test_cli_immediate_approval_requires_explicit_yes_and_shows_full_action():
    shown = []
    prompts = []
    action = "Send this full message to the recipient."

    allowed = _cli_approval_fn(
        "gmail_send",
        {"task": action},
        input_fn=lambda prompt: prompts.append(prompt) or "yes",
        output_fn=shown.append,
    )

    assert allowed is True
    assert action in shown[0]
    assert prompts == ["Approve this exact action? [y/N] "]


def test_cli_immediate_approval_defaults_to_denial():
    for answer in ("", "no", "yep", "approve"):
        assert _cli_approval_fn(
            "gmail_send", {"task": "send"}, input_fn=lambda _prompt, answer=answer: answer,
            output_fn=lambda _message: None,
        ) is False


def test_cli_immediate_approval_denies_when_input_is_closed():
    def closed(_prompt):
        raise EOFError

    assert _cli_approval_fn("gmail_send", {"task": "send"}, input_fn=closed, output_fn=lambda _: None) is False


def test_confirmation_cli_exposes_all_durable_queue_decisions():
    parser = build_parser()
    assert parser.parse_args(["confirm", "list"]).confirm_command == "list"
    assert parser.parse_args(["confirm", "approve", "confirmation.x"]).id == "confirmation.x"
    assert parser.parse_args(["confirm", "deny", "confirmation.x"]).confirm_command == "deny"
    snooze = parser.parse_args(["confirm", "snooze", "confirmation.x", "--days", "3"])
    assert snooze.confirm_command == "snooze"
    assert snooze.days == 3
    assert parser.parse_args(["confirm", "approve-all"]).confirm_command == "approve-all"


def test_approve_all_resolves_every_pending_confirmation(monkeypatch, capsys):
    from lisan.tools import adjutant_confirmations

    pending = [
        {"id": "confirmation.one", "task_id": "task.one"},
        {"id": "confirmation.two", "task_id": "task.two"},
    ]
    approved = []
    monkeypatch.setattr(adjutant_confirmations, "list_pending", lambda _db: pending)

    def approve(_vault, confirmation_id, *, db_path=None):
        approved.append(confirmation_id)
        return {"id": confirmation_id, "task_id": f"task.{confirmation_id.rsplit('.', 1)[-1]}"}

    monkeypatch.setattr(adjutant_confirmations, "approve_confirmation", approve)
    assert main(["confirm", "approve-all"]) == 0
    assert approved == ["confirmation.one", "confirmation.two"]
    assert "Approved 2 of 2 pending confirmations" in capsys.readouterr().out


def test_approve_all_reports_partial_errors_and_continues(monkeypatch, capsys):
    from lisan.tools import adjutant_confirmations

    pending = [{"id": "confirmation.bad"}, {"id": "confirmation.good"}]
    seen = []
    monkeypatch.setattr(adjutant_confirmations, "list_pending", lambda _db: pending)

    def approve(_vault, confirmation_id, *, db_path=None):
        seen.append(confirmation_id)
        if confirmation_id.endswith("bad"):
            raise ValueError("already resolved")
        return {"id": confirmation_id, "task_id": "task.good"}

    monkeypatch.setattr(adjutant_confirmations, "approve_confirmation", approve)
    assert main(["confirm", "approve-all"]) == 1
    assert seen == ["confirmation.bad", "confirmation.good"]
    assert "Approved 1 of 2 pending confirmations" in capsys.readouterr().out


def test_noninteractive_tool_registry_does_not_auto_approve_skills(tmp_path, monkeypatch):
    seen = {}

    def load_handlers(_skills_dir, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(execution_tools, "load_skill_handlers", load_handlers)
    execution_tools.build_tool_handlers(vault=tmp_path, config={"providers": {}})
    assert seen["approval_fn"] is None
