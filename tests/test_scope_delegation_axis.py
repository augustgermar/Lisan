"""The delegation axis is its own field, and absence of it is reportable.

WO-ADJUTANT's central defect: delegation was grafted onto the life-dimension
enum under the name ``arena``, so ``primer/intent.md`` declared eight areas
of responsibility while records carried life dimensions, the overlap was
empty, and every task resolved through ``defaults``. It stayed invisible for
a month because ``files.arena`` was ``COALESCE(arena, domain_primary,
arena_primary)`` — an absent authority declaration silently became a
plausible-looking life dimension, so the gate never reported a gap.

These tests pin the split so it cannot be re-conflated:

1. ``scope`` never falls back to ``domain`` — at the reader, and in the index.
2. An unscoped record resolves to REPORT_ONLY with the rule ``no_scope``,
   distinguishable from a declared-but-unlisted scope.
3. ``defaults`` cannot grant execute, however it is written.
4. The legacy ``arenas`` spelling keeps resolving (adopted documents survive).
5. Casing drift cannot cost a record its authority.
6. The capture pipeline never assigns a scope.
7. ``lisan intent scopes`` names the eight-declared/zero-reachable gap.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.adjutant_gate import gate
from lisan.tools.db import connect as db_connect
from lisan.tools.intent import (
    EXECUTE,
    REPORT_ONLY,
    _record_known_hash,
    init_intent,
    intent_path,
    load_intent,
    parse_intent,
    resolve_delegation,
    validate_intent_text,
)
from lisan.tools.intent_scopes import scope_coverage
from lisan.tools.rebuild_index import ensure_index_schema, index_single_record
from lisan.tools.record_factory import new_open_loop
from lisan.tools.scope import declared_scopes, normalize_scope, record_scope


# ---------------------------------------------------------------------------
# 1. No fallback, anywhere

def test_record_scope_never_falls_back_to_domain():
    """The one line of convenience that caused the whole defect."""
    fm = {"domain_primary": "work", "arena_primary": "work", "arena": "work"}
    assert record_scope(fm) == "", "a life dimension must never masquerade as a delegation scope"
    assert record_scope({"scope": "greenhouse"}) == "greenhouse"
    assert record_scope({}) == ""


def test_index_keeps_scope_null_while_arena_still_mirrors_domain(tmp_path):
    """`arena` may coalesce from domain — same axis, older name. `scope` may not."""
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    conn = db_connect(tmp_path / "i.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)

    plain = new_open_loop(vault, "Unscoped work", domain_primary="work")
    scoped = new_open_loop(vault, "Scoped work", domain_primary="work", scope="greenhouse")
    for record in (plain, scoped):
        assert index_single_record(record.path, vault, conn)
    conn.commit()

    rows = {r["id"]: r for r in conn.execute("SELECT id, arena, scope, domain_primary FROM files")}
    unscoped_row = rows["open_loop.unscoped-work"]
    assert unscoped_row["domain_primary"] == "work"
    assert unscoped_row["arena"] == "work"          # legacy mirror, unchanged
    assert unscoped_row["scope"] is None            # the whole point
    assert rows["open_loop.scoped-work"]["scope"] == "greenhouse"
    conn.close()


# ---------------------------------------------------------------------------
# 2 & 3. Absence is reportable; defaults cannot grant

BASE = {
    "defaults": {"mode": "report_only"},
    "scopes": {"greenhouse": {"mode": "execute", "capabilities": ["read_files", "write_files"]}},
    "global": {},
}


def test_no_scope_is_distinguishable_from_unlisted_scope():
    """Different owner remedies, so different rules: tag the record vs add a rule."""
    absent = resolve_delegation(BASE, "", "read_files")
    unlisted = resolve_delegation(BASE, "observatory", "read_files")
    assert absent.decision == REPORT_ONLY and absent.rule == "no_scope"
    assert unlisted.decision == REPORT_ONLY and unlisted.rule == "defaults.mode=report_only"
    assert absent.rule != unlisted.rule


def test_declared_scope_grants_execute():
    verdict = resolve_delegation(BASE, "greenhouse", "read_files")
    assert verdict.decision == EXECUTE and verdict.rule == "scopes.greenhouse.mode=execute"


@pytest.mark.parametrize(
    "defaults",
    [
        {"mode": "execute"},
        {"mode": "execute", "capabilities": ["read_files", "write_files", "run_local_scripts"]},
        {"mode": "execute", "confirm_required": []},
    ],
)
def test_defaults_can_never_grant_execute(defaults):
    """The fail-safe that made a month of mismatch harmless rather than
    dangerous. An owner who widens defaults because 'nothing ever runs' must
    not thereby authorize everything."""
    delegations = {**BASE, "defaults": defaults}
    for scope in ("", "observatory", "anything-at-all"):
        verdict = resolve_delegation(delegations, scope, "read_files")
        assert verdict.decision == REPORT_ONLY, f"defaults granted execute for {scope!r}"


# ---------------------------------------------------------------------------
# 4. The legacy spelling keeps working

def test_legacy_arenas_key_still_resolves():
    """An owner's adopted authority document must not be invalidated by a
    vocabulary change in the code."""
    legacy = {
        "defaults": {"mode": "report_only"},
        "arenas": {"greenhouse": {"mode": "execute", "capabilities": ["read_files"]}},
    }
    verdict = resolve_delegation(legacy, "greenhouse", "read_files")
    assert verdict.decision == EXECUTE
    assert verdict.rule == "arenas.greenhouse.mode=execute", "messages should echo the owner's spelling"
    assert declared_scopes(legacy) == {"greenhouse": {"mode": "execute", "capabilities": ["read_files"]}}


def test_both_spellings_at_once_is_flagged_not_silently_resolved():
    both = {
        "defaults": {"mode": "report_only"},
        "scopes": {"greenhouse": {"mode": "execute", "capabilities": ["read_files"]}},
        "arenas": {"greenhouse": {"mode": "report_only"}},
    }
    doc = _intent_text(both)
    issues = validate_intent_text(doc)
    assert any("both 'scopes' and 'arenas'" in issue for issue in issues)
    # ...and scopes wins per key, so the owner is warned rather than overruled.
    assert resolve_delegation(both, "greenhouse", "read_files").decision == EXECUTE


# ---------------------------------------------------------------------------
# 5. Casing drift cannot cost a record its authority

@pytest.mark.parametrize("written", ["Greenhouse", "GREENHOUSE", "  greenhouse  ", "Greenhouse "])
def test_scope_matching_is_case_and_whitespace_insensitive(written):
    """Observed drift included values like "Lisan System" beside "system". A
    delegation that misses because of a capital letter looks like a considered
    rule and behaves like no rule."""
    assert normalize_scope(written) == "greenhouse"
    assert resolve_delegation(BASE, written, "read_files").decision == EXECUTE


def test_declared_scope_names_are_normalized_too():
    shouty = {"defaults": {"mode": "report_only"}, "scopes": {"Greenhouse": {"mode": "execute", "capabilities": ["read_files"]}}}
    assert resolve_delegation(shouty, "greenhouse", "read_files").decision == EXECUTE


# ---------------------------------------------------------------------------
# 6. Capture never assigns a scope

def test_writer_proposed_scope_is_dropped(tmp_path):
    from lisan.tools.record_fanout import fanout_open_loops

    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    writer = {
        "open_loops_to_create": [
            {
                "title": "Do the thing",
                "next_action": "Do it",
                "owner": "user",
                "scope": "greenhouse",
                "task": {"task_kind": "draft", "task_payload": {"title": "t", "instructions": "i"},
                         "scope": "greenhouse"},
            }
        ]
    }
    fanout_open_loops(vault, writer, "drafts/x.md")
    fm = load_markdown(next((vault / "open_loops").glob("*.md"))).frontmatter
    assert fm["task_kind"] == "draft", "the tasking itself survives"
    assert "scope" not in fm, "the model does not get to grant itself authority"


def test_gate_reports_unscoped_captured_task():
    intent = parse_intent(_intent_text(BASE))
    verdict = gate({"scope": "", "task_kinds": ["draft"], "blocked_contexts": []}, intent)
    assert verdict.decision == REPORT_ONLY and verdict.rule == "no_scope"


# ---------------------------------------------------------------------------
# 7. The instrument that would have caught it in one command

def test_scope_coverage_names_the_declared_but_unreachable_gap(tmp_path):
    """Reproduces the live shape: every declared rule unreachable, and the
    records that exist carrying no scope at all."""
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    init_intent(vault)
    delegations = {
        "defaults": {"mode": "report_only"},
        "scopes": {name: {"mode": "report_only", "capabilities": ["read_files"]}
                   for name in ("greenhouse", "kiln", "apiary")},
    }
    _install_delegations(vault, delegations)

    db = tmp_path / "i.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    for title in ("First loop", "Second loop"):
        record = new_open_loop(vault, title, domain_primary="work")
        index_single_record(record.path, vault, conn)
    conn.commit()
    conn.close()

    data = scope_coverage(load_intent(vault), db)
    assert data["declared"] == ["apiary", "greenhouse", "kiln"]
    assert data["present"] == {}
    assert data["declared_unused"] == ["apiary", "greenhouse", "kiln"]
    assert data["unscoped_pollable"] == 2
    assert data["database_error"] is None


def test_scope_coverage_sees_a_reachable_scope(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    init_intent(vault)
    _install_delegations(vault, BASE)

    db = tmp_path / "i.sqlite"
    conn = db_connect(db)
    conn.row_factory = sqlite3.Row
    ensure_index_schema(conn)
    record = new_open_loop(vault, "Scoped loop", scope="greenhouse")
    index_single_record(record.path, vault, conn)
    stray = new_open_loop(vault, "Stray loop", scope="observatory")
    index_single_record(stray.path, vault, conn)
    conn.commit()
    conn.close()

    data = scope_coverage(load_intent(vault), db)
    assert data["present"] == {"greenhouse": 1, "observatory": 1}
    assert data["present_undeclared"] == ["observatory"]
    assert data["declared_unused"] == []


# ---------------------------------------------------------------------------
# The shipped template must not teach the shape that caused this

def test_shipped_template_uses_scopes_and_no_stale_example_names():
    from lisan.tools.intent import default_intent_document

    doc = default_intent_document()
    intent = parse_intent(doc)
    assert "scopes" in intent.delegations, "the template must teach the canonical key"
    assert "arenas" not in intent.delegations
    # The old template shipped `example-project` and `legal` — neither is an
    # owner's real area of responsibility, and `legal` looked authoritative
    # enough to copy verbatim. Placeholders must be obviously placeholders.
    assert "example-project" not in doc
    for name in intent.delegations["scopes"]:
        assert name.startswith("rename-me"), f"template scope {name!r} is copyable as-is"
    assert validate_intent_text(doc) == []


def test_template_documents_the_domain_scope_distinction():
    from lisan.tools.intent import default_intent_document

    doc = default_intent_document()
    assert "domain_primary" in doc and "lisan intent scopes" in doc
    assert "Nothing infers a scope" in doc


# ---------------------------------------------------------------------------
# helpers

def _intent_text(delegations: dict) -> str:
    from lisan.tools.intent import default_intent_document

    doc = default_intent_document(today="2026-07-29")
    start = doc.index("```json")
    end = doc.index("```", start + 7) + 3
    return doc[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + doc[end:]


def _install_delegations(vault, delegations: dict) -> None:
    path = intent_path(vault)
    doc = load_markdown(path)
    body = doc.body
    start = body.index("```json")
    end = body.index("```", start + 7) + 3
    body = body[:start] + "```json\n" + json.dumps(delegations, indent=2) + "\n```" + body[end:]
    fm = dict(doc.frontmatter)
    fm.update(created="2026-07-29", updated="2026-07-29", review_after="2026-10-29")
    path.write_text(dump_markdown(fm, body), encoding="utf-8")
    _record_known_hash(vault)
