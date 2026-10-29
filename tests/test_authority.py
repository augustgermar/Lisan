"""The one place that decides whether an action may happen."""
from __future__ import annotations

import json

import pytest

from lisan.tools import authority
from lisan.tools.authority import ALLOW, ASK, DENY, rule
from tests.intent_helpers import adopt_intent


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    adopt_intent(tmp_path)
    monkeypatch.delenv("LISAN_NO_OUTBOUND", raising=False)
    monkeypatch.setenv("LISAN_ALLOW_TEST_OUTBOUND", "1")
    return tmp_path


def test_unknown_actions_are_denied(vault):
    assert rule("launch_rockets", vault=vault).decision == DENY


def test_outbound_with_a_channel_asks_and_without_one_is_denied(vault):
    assert rule("send_email", vault=vault, approval_channel=True).decision == ASK
    denied = rule("send_email", vault=vault, approval_channel=False)
    assert (denied.decision, denied.rule) == (DENY, "approval.no_channel")


def test_never_rules_outrank_everything(vault):
    path = vault / "primer" / "intent.md"
    text = path.read_text(encoding="utf-8")
    assert '"send_outbound_message": "confirm_always"' in text
    path.write_text(text.replace('"send_outbound_message": "confirm_always"', '"send_outbound_message": "never"'),
                    encoding="utf-8")
    ruling = rule("send_email", vault=vault, approval_channel=True)
    assert ruling.decision == DENY and ruling.rule.startswith("intent.")


def test_unadopted_intent_fails_closed(tmp_path):
    assert rule("run_codex", vault=tmp_path).decision == DENY


def test_kill_switch_beats_approval(vault, monkeypatch):
    monkeypatch.setenv("LISAN_NO_OUTBOUND", "1")
    assert rule("skill.outbound", vault=vault, approval_channel=True).rule == "outbound.kill_switch"


def test_tiers_gate_drive_actions(vault):
    assert rule("drive.enrich", vault=vault, config={"drive": {"action_tier": 0}}).decision == DENY
    assert rule("drive.enrich", vault=vault, config={"drive": {"action_tier": 3}}).decision == ALLOW


def test_every_ruling_is_audited_with_its_rule(vault):
    rule("send_email", vault=vault, approval_channel=True, detail={"to": "a@b.c"})
    entry = json.loads((vault / "logs" / "authority.jsonl").read_text().splitlines()[-1])
    assert entry["action"] == "send_email" and entry["rule"] == "approval.required"
    assert entry["detail"]["to"] == "a@b.c"


def test_the_policy_surface_is_inspectable():
    surface = authority.registered_actions()
    assert surface["send_email"]["outbound"] and surface["send_email"]["approval"]
