"""Commander's intent: parsing, validation, delegation resolution, versioning.

The delegation matrix here is the contract the Adjutant gate (WO-ADJUTANT
step 3) builds on: default deny, most restrictive wins, never-rules
override everything.
"""
from __future__ import annotations

import json

import pytest

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.tools.intent import (
    CONFIRM,
    DENY,
    EXECUTE,
    REPORT_ONLY,
    IntentError,
    default_intent_document,
    detect_out_of_band_edit,
    init_intent,
    intent_history_dir,
    intent_path,
    list_intent_history,
    load_intent,
    parse_intent,
    resolve_capabilities,
    resolve_delegation,
    snapshot_intent,
    validate_intent_text,
)

DELEGATIONS = {
    "defaults": {"mode": "report_only"},
    "arenas": {
        "lisan-dev": {
            "mode": "execute",
            "capabilities": ["run_local_scripts", "read_files", "write_files", "web_research"],
            "confirm_required": ["git_push", "publish"],
        },
        "finance": {
            "mode": "report_only",
            "capabilities": ["read_files", "web_research"],
            "confirm_required": ["*"],
        },
        "legal": {
            "mode": "report_only",
            "capabilities": ["read_files"],
            "confirm_required": ["*"],
            "outbound_comms": "never",
        },
        "attic": {"mode": "disabled"},
    },
    "global": {
        "spend_money": "confirm_always",
        "send_outbound_message": "confirm_always",
        "delete_files": "confirm_always",
        "max_task_wall_seconds": 600,
        "max_tasks_per_cycle": 5,
    },
}


def _intent_text(delegations: dict | None = None, *, drop_section: str | None = None) -> str:
    doc = default_intent_document(today="2026-07-23")
    if delegations is not None:
        parsed = load_markdown_text(doc)
        body = parsed.body
        start = body.index("```json")
        end = body.index("```", start + 7) + 3
        body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
        doc = dump_markdown(parsed.frontmatter, body)
    if drop_section:
        parsed = load_markdown_text(doc)
        lines = parsed.body.splitlines()
        out, skipping = [], False
        for line in lines:
            if line.startswith("# "):
                skipping = line[2:].strip() == drop_section
            if not skipping:
                out.append(line)
        doc = dump_markdown(parsed.frontmatter, "\n".join(out))
    return doc


def load_markdown_text(text: str):
    from lisan.frontmatter import parse_markdown

    return parse_markdown(text)


# ---------------------------------------------------------------------------
# Delegation resolution matrix

def test_execute_arena_granted_capability_executes():
    v = resolve_delegation(DELEGATIONS, "lisan-dev", "run_local_scripts")
    assert v.decision == EXECUTE


def test_execute_arena_confirm_required_capability_confirms():
    v = resolve_delegation(DELEGATIONS, "lisan-dev", "git_push")
    assert v.decision == CONFIRM
    assert "confirm_required" in v.rule


def test_execute_arena_ungranted_capability_falls_to_report_only():
    v = resolve_delegation(DELEGATIONS, "lisan-dev", "delete_files")
    # delete_files is globally confirm_always AND ungranted in the arena:
    # most restrictive wins -> report_only outranks confirm.
    assert v.decision == REPORT_ONLY


def test_report_only_arena_never_executes():
    assert resolve_delegation(DELEGATIONS, "finance", "read_files").decision == REPORT_ONLY


def test_global_confirm_always_outranks_arena_execute():
    grants = {
        "defaults": {"mode": "report_only"},
        "arenas": {"shop": {"mode": "execute", "capabilities": ["spend_money"]}},
        "global": {"spend_money": "confirm_always"},
    }
    v = resolve_delegation(grants, "shop", "spend_money")
    assert v.decision == CONFIRM
    assert v.rule == "global.spend_money=confirm_always"


def test_report_only_mode_outranks_global_confirm():
    v = resolve_delegation(DELEGATIONS, "finance", "spend_money")
    assert v.decision == REPORT_ONLY


def test_disabled_arena_is_denied():
    v = resolve_delegation(DELEGATIONS, "attic", "read_files")
    assert v.decision == DENY
    assert "disabled" in v.rule


def test_outbound_comms_never_denies_send():
    v = resolve_delegation(DELEGATIONS, "legal", "send_outbound_message")
    assert v.decision == DENY
    assert "outbound_comms=never" in v.rule


def test_never_rules_beat_confirm_required_grants():
    # The seam a future edit will hit: naming a capability in an arena's
    # confirm_required is a grant-with-confirmation — but never-rules
    # (arena outbound_comms=never, global never) still outrank it. DENY,
    # not CONFIRM.
    grants = {
        "defaults": {"mode": "report_only"},
        "arenas": {
            "legal": {
                "mode": "execute",
                "capabilities": ["read_files"],
                "confirm_required": ["send_outbound_message", "delete_files"],
                "outbound_comms": "never",
            }
        },
        "global": {"delete_files": "never"},
    }
    v = resolve_delegation(grants, "legal", "send_outbound_message")
    assert v.decision == DENY
    assert "outbound_comms=never" in v.rule
    v = resolve_delegation(grants, "legal", "delete_files")
    assert v.decision == DENY
    assert v.rule == "global.delete_files=never"


def test_global_never_denies_everywhere():
    grants = {
        "defaults": {"mode": "report_only"},
        "arenas": {"dev": {"mode": "execute", "capabilities": ["delete_files"]}},
        "global": {"delete_files": "never"},
    }
    assert resolve_delegation(grants, "dev", "delete_files").decision == DENY


def test_unlisted_arena_uses_defaults_report_only():
    v = resolve_delegation(DELEGATIONS, "garden", "read_files")
    assert v.decision == REPORT_ONLY
    assert "defaults" in v.rule


def test_defaults_disabled_denies_unlisted_arena():
    grants = {"defaults": {"mode": "disabled"}, "arenas": {}, "global": {}}
    assert resolve_delegation(grants, "anything", "read_files").decision == DENY


def test_defaults_execute_still_grants_nothing():
    # Default deny: an execute default without an arena capability list
    # never actually executes.
    grants = {"defaults": {"mode": "execute"}, "arenas": {}, "global": {}}
    assert resolve_delegation(grants, "anything", "read_files").decision == REPORT_ONLY


def test_capability_set_takes_most_restrictive():
    v = resolve_capabilities(DELEGATIONS, "lisan-dev", ["read_files", "git_push"])
    assert v.decision == CONFIRM
    v = resolve_capabilities(DELEGATIONS, "lisan-dev", ["read_files", "web_research"])
    assert v.decision == EXECUTE
    v = resolve_capabilities(DELEGATIONS, "legal", ["read_files", "send_outbound_message"])
    assert v.decision == DENY


# ---------------------------------------------------------------------------
# Validation

def test_template_validates_clean():
    assert validate_intent_text(default_intent_document()) == []


@pytest.mark.parametrize("section", ["Mission", "Priorities", "Standing Delegations", "Escalation Rules", "Never"])
def test_missing_section_is_an_issue(section):
    issues = validate_intent_text(_intent_text(drop_section=section))
    assert any(section in issue for issue in issues)


def test_unknown_capability_is_an_issue():
    bad = {
        "defaults": {"mode": "report_only"},
        "arenas": {"dev": {"mode": "execute", "capabilities": ["launch_rockets"]}},
        "global": {},
    }
    issues = validate_intent_text(_intent_text(bad))
    assert any("launch_rockets" in issue for issue in issues)


def test_invalid_mode_is_an_issue():
    bad = {"defaults": {"mode": "yolo"}, "arenas": {}, "global": {}}
    issues = validate_intent_text(_intent_text(bad))
    assert any("defaults.mode" in issue for issue in issues)


def test_unknown_global_rule_is_an_issue():
    bad = {"defaults": {"mode": "report_only"}, "arenas": {}, "global": {"frobnicate": "confirm_always"}}
    issues = validate_intent_text(_intent_text(bad))
    assert any("frobnicate" in issue for issue in issues)


def test_malformed_delegations_json_is_structural():
    text = default_intent_document().replace('"defaults"', '"defaults', 1)
    issues = validate_intent_text(text)
    assert issues and "JSON" in issues[0]


def test_version_must_be_positive_int():
    doc = load_markdown_text(default_intent_document())
    fm = dict(doc.frontmatter)
    fm["version"] = "three"
    issues = validate_intent_text(dump_markdown(fm, doc.body))
    assert any("version" in issue for issue in issues)


# ---------------------------------------------------------------------------
# Lifecycle

def test_init_and_load(tmp_path):
    path = init_intent(tmp_path)
    assert path == intent_path(tmp_path)
    intent = load_intent(tmp_path)
    assert intent.version == 1
    assert intent.delegations["defaults"]["mode"] == "report_only"
    with pytest.raises(IntentError):
        init_intent(tmp_path)  # refuses to clobber


def test_load_fails_closed_on_invalid(tmp_path):
    init_intent(tmp_path)
    p = intent_path(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("# Never", "# Nope"), encoding="utf-8")
    with pytest.raises(IntentError):
        load_intent(tmp_path)


def test_load_fails_closed_when_missing(tmp_path):
    with pytest.raises(IntentError):
        load_intent(tmp_path)


def test_snapshot_preserves_prior_version(tmp_path):
    init_intent(tmp_path)
    snap = snapshot_intent(tmp_path, timestamp="20260723T000000Z")
    assert snap.parent == intent_history_dir(tmp_path)
    assert snap.name == "intent-20260723T000000Z.md"
    assert list_intent_history(tmp_path) == [snap]
    # Same-second snapshots do not clobber each other.
    snap2 = snapshot_intent(tmp_path, timestamp="20260723T000000Z")
    assert snap2 != snap and snap2.exists()


def test_out_of_band_edit_is_detected_and_absorbed(tmp_path):
    init_intent(tmp_path)
    assert detect_out_of_band_edit(tmp_path) is False
    p = intent_path(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("_Second priority._", "Keep the garden alive."), encoding="utf-8")
    assert detect_out_of_band_edit(tmp_path) is True
    # Snapshot taken, version bumped, hash re-recorded.
    assert len(list_intent_history(tmp_path)) == 1
    assert load_intent(tmp_path).version == 2
    assert detect_out_of_band_edit(tmp_path) is False


def test_parse_hash_is_stable():
    text = default_intent_document(today="2026-07-23")
    assert parse_intent(text).content_hash == parse_intent(text).content_hash


# ---------------------------------------------------------------------------
# Vault validator integration

def test_validate_vault_flags_broken_intent(tmp_path):
    from lisan.tools.validator import validate_vault

    vault = tmp_path / "vault"
    (vault / "primer").mkdir(parents=True)
    init_intent(vault)
    assert validate_vault(vault).ok
    p = intent_path(vault)
    p.write_text(p.read_text(encoding="utf-8").replace("# Mission", "# Vibes"), encoding="utf-8")
    report = validate_vault(vault)
    assert not report.ok
    assert any("intent.md" in issue.message for issue in report.issues)
