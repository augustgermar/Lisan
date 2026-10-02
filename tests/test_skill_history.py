"""Skill history, rollback and portability (learning loop step 1).

Claims: nothing the loop (or the owner) changes is unrecoverable; the owner's
own text is always one command away; import cannot be turned against the
machine (traversal, links, bundled code); and the loader never mistakes the
history for skills.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from lisan.tools import skill_history as H
from lisan.tools.skill_loader import load_skills

OWNER_SKILL = "---\nname: server-audit\ndescription: Use when auditing a server.\nversion: 1.0.0\n---\n# Audit\n1. check disks\n"


@pytest.fixture()
def skills(tmp_path):
    root = tmp_path / "skills"
    (root / "server-audit").mkdir(parents=True)
    (root / "server-audit" / "SKILL.md").write_text(OWNER_SKILL, encoding="utf-8")
    (root / "server-audit" / "references").mkdir()
    (root / "server-audit" / "references" / "ports.md").write_text("22 ssh\n", encoding="utf-8")
    return root


def edit(skills, text="2. check memory\n", name="server-audit"):
    path = skills / name / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


# ── frontmatter provenance ──────────────────────────────────────────────────

def test_nested_metadata_now_parses():
    from lisan.tools.skill_format import parse_frontmatter

    data, _ = parse_frontmatter(
        "---\nname: x\ndescription: d\nmetadata:\n  origin: agent\n  tainted: true\n  sources: owner, web\nversion: 1\n---\nb\n"
    )
    assert data["metadata"] == {"origin": "agent", "tainted": True, "sources": "owner, web"}
    assert data["version"] == 1  # keys after the block are unaffected


def test_block_lists_still_parse_after_the_metadata_fix():
    from lisan.tools.skill_format import parse_frontmatter

    data, _ = parse_frontmatter("---\nname: x\ndescription: d\nallowed-tools:\n  - Read\n  - Bash\n---\nb\n")
    assert data["allowed-tools"] == ["Read", "Bash"]


def test_provenance_defaults_to_owner_and_reads_agent_metadata(skills):
    assert H.read_provenance(skills, "server-audit")["origin"] == "owner"
    (skills / "learned").mkdir()
    (skills / "learned" / "SKILL.md").write_text(
        "---\nname: learned\ndescription: Use when x.\nmetadata:\n  origin: agent\n  status: provisional\n"
        "  tainted: true\n  sources: owner, email\n---\nb\n", encoding="utf-8")
    prov = H.read_provenance(skills, "learned")
    assert prov["origin"] == "agent" and prov["status"] == "provisional" and prov["tainted"] is True
    assert prov["sources"] == ["owner", "email"] and prov["pinned"] is False


# ── snapshots ───────────────────────────────────────────────────────────────

def test_the_first_snapshot_of_an_owner_skill_is_v0(skills):
    version = H.snapshot_skill(skills, "server-audit", reason="before the loop edits it", actor="loop")
    (entry,) = H.list_history(skills, "server-audit")
    assert entry["version_id"] == version and entry["label"] == "v0" and entry["actor"] == "loop"
    assert (skills / ".history" / "server-audit" / version / "references" / "ports.md").is_file()


def test_an_agent_origin_skill_gets_no_v0(skills):
    (skills / "learned").mkdir()
    (skills / "learned" / "SKILL.md").write_text(
        "---\nname: learned\ndescription: d\nmetadata:\n  origin: agent\n---\nb\n", encoding="utf-8")
    H.snapshot_skill(skills, "learned", reason="x")
    assert H.list_history(skills, "learned")[0]["label"] is None


def test_an_unchanged_skill_is_not_snapshotted_twice(skills):
    first = H.snapshot_skill(skills, "server-audit", reason="a")
    assert H.snapshot_skill(skills, "server-audit", reason="b") == first
    assert len(H.list_history(skills, "server-audit")) == 1
    edit(skills)
    assert H.snapshot_skill(skills, "server-audit", reason="c") != first
    assert len(H.list_history(skills, "server-audit")) == 2


def test_two_snapshots_in_one_second_get_distinct_ids(skills):
    ids = set()
    for i in range(3):
        edit(skills, f"line {i}\n")
        ids.add(H.snapshot_skill(skills, "server-audit", reason=str(i)))
    assert len(ids) == 3


def test_snapshots_record_the_events_that_justified_the_change(skills):
    H.snapshot_skill(skills, "server-audit", reason="learned a trap", actor="reviewer", events=["turn:job.1", "plan:p:r0:completed"])
    entry = H.list_history(skills, "server-audit")[0]
    assert entry["events"] == ["turn:job.1", "plan:p:r0:completed"]
    assert H.read_log(skills, "server-audit")[0]["reason"] == "learned a trap"


# ── diff ────────────────────────────────────────────────────────────────────

def test_since_owner_measures_drift_from_your_own_text(skills):
    assert H.diff_skill(skills, "server-audit", since="owner") == ""  # never edited by the loop
    H.snapshot_skill(skills, "server-audit", reason="r", actor="loop")
    edit(skills, "2. check memory\n")
    diff = H.diff_skill(skills, "server-audit", since="owner")
    assert "+2. check memory" in diff and "-1. check disks" not in diff and "@v0" in diff


def test_diff_covers_reference_files_and_names_versions(skills):
    v = H.snapshot_skill(skills, "server-audit", reason="r")
    (skills / "server-audit" / "references" / "ports.md").write_text("22 ssh\n443 https\n", encoding="utf-8")
    assert "+443 https" in H.diff_skill(skills, "server-audit", since=v)
    assert H.diff_skill(skills, "server-audit") != ""
    with pytest.raises(H.SkillHistoryError, match="no version"):
        H.diff_skill(skills, "server-audit", since="nope")
    with pytest.raises(H.SkillHistoryError, match="no history yet"):
        H.diff_skill(_fresh(skills, "plain"), "plain")


def _fresh(skills, name):
    (skills / name).mkdir()
    (skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nb\n", encoding="utf-8")
    return skills


# ── rollback ────────────────────────────────────────────────────────────────

def test_rollback_restores_your_text_and_is_itself_undoable(skills):
    H.snapshot_skill(skills, "server-audit", reason="r", actor="loop")  # v0
    edit(skills, "2. LOOP LINE\n")
    saved = H.rollback_skill(skills, "server-audit", "v0")
    assert (skills / "server-audit" / "SKILL.md").read_text(encoding="utf-8") == OWNER_SKILL
    assert (skills / "server-audit" / "references" / "ports.md").is_file()
    assert saved  # the loop's version was kept
    H.rollback_skill(skills, "server-audit", saved)  # undo the rollback
    assert "LOOP LINE" in (skills / "server-audit" / "SKILL.md").read_text(encoding="utf-8")
    assert "rollback" in {e["action"] for e in H.read_log(skills, "server-audit")}


def test_v0_survives_a_rollback_so_you_can_return_to_your_text_again_and_again(skills):
    """Found on the first real rollback: the rollback's own log entry named the same
    version and overwrote its label, so `v0` stopped resolving after one use."""
    H.snapshot_skill(skills, "server-audit", reason="r", actor="loop")  # v0
    for attempt in range(3):
        edit(skills, f"loop edit {attempt}\n")
        H.rollback_skill(skills, "server-audit", "v0")
        assert (skills / "server-audit" / "SKILL.md").read_text(encoding="utf-8") == OWNER_SKILL
        assert [h["label"] for h in H.list_history(skills, "server-audit") if h["label"] == "v0"] == ["v0"]
    assert H.diff_skill(skills, "server-audit", since="owner") == ""
    edit(skills, "one more\n")
    assert "+one more" in H.diff_skill(skills, "server-audit", since="owner")  # still measures drift from the owner's text


def test_logs_written_before_the_fix_still_resolve_their_labels(skills):
    """Rollback entries used to carry `version_id` (the restored one) and clobbered
    the snapshot's label. Logs already on disk contain such entries; reading them
    must not depend on every writer having been fixed."""
    v = H.snapshot_skill(skills, "server-audit", reason="r", actor="loop")  # labelled v0
    H._log(skills, {"skill": "server-audit", "action": "rollback", "version_id": v, "actor": "owner", "reason": "legacy shape"})
    (entry,) = H.list_history(skills, "server-audit")
    assert entry["label"] == "v0" and entry["action"] == "snapshot"
    assert H.diff_skill(skills, "server-audit", since="owner") == ""  # `v0` still resolves


def test_the_label_of_a_snapshot_comes_from_the_entry_that_created_it(skills):
    v = H.snapshot_skill(skills, "server-audit", reason="the first", actor="loop")
    H.rollback_skill(skills, "server-audit", v)
    (entry,) = [h for h in H.list_history(skills, "server-audit") if h["version_id"] == v]
    assert entry["label"] == "v0" and entry["reason"] == "the first" and entry["action"] == "snapshot"


def test_rollback_to_an_unknown_version_changes_nothing(skills):
    with pytest.raises(H.SkillHistoryError):
        H.rollback_skill(skills, "server-audit", "20250101T000000Z")
    assert (skills / "server-audit" / "SKILL.md").read_text(encoding="utf-8") == OWNER_SKILL


# ── archive and pin ─────────────────────────────────────────────────────────

def test_archive_removes_from_service_without_destroying(skills):
    dest = H.archive_skill(skills, "server-audit", reason="superseded")
    assert not (skills / "server-audit").exists() and (dest / "SKILL.md").is_file()
    assert "server-audit" not in {s["name"] for s in load_skills(skills)}
    assert H.list_history(skills, "server-audit")  # and its history survives


def test_pin_and_unpin(skills):
    assert H.read_provenance(skills, "server-audit")["pinned"] is False
    H.pin_skill(skills, "server-audit")
    assert H.is_pinned(skills, "server-audit") and H.read_provenance(skills, "server-audit")["pinned"]
    H.pin_skill(skills, "server-audit", pinned=False)
    assert not H.is_pinned(skills, "server-audit")
    with pytest.raises(H.SkillHistoryError):
        H.pin_skill(skills, "no-such-skill")


def test_the_loader_never_mistakes_history_or_archive_for_skills(skills):
    H.snapshot_skill(skills, "server-audit", reason="r")
    H.archive_skill(skills, "server-audit")
    _fresh(skills, "live")
    assert [s["name"] for s in load_skills(skills)] == ["live"]


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "x" * 80, "spa ce"])
def test_unsafe_skill_names_are_refused(skills, bad):
    with pytest.raises(H.SkillHistoryError):
        H.snapshot_skill(skills, bad, reason="x")


# ── export / import ─────────────────────────────────────────────────────────

def _other_machine(tmp_path):
    root = tmp_path / "work-skills"
    root.mkdir()
    return root


def test_export_import_round_trip_carries_provenance_and_history(skills, tmp_path):
    H.snapshot_skill(skills, "server-audit", reason="owner's original", actor="loop")  # v0
    edit(skills)
    (tmp_path / "out").mkdir()
    archive = H.export_skill(skills, "server-audit", tmp_path / "out")  # a directory: named for the skill
    assert archive.name == "server-audit.skill.tar.gz" and archive.is_file()

    work = _other_machine(tmp_path)
    out = H.import_skill(work, archive)
    assert out == {"name": "server-audit", "stripped_code": [], "history_versions": 1, "replaced": False}
    assert "check memory" in (work / "server-audit" / "SKILL.md").read_text(encoding="utf-8")
    # the label survived, so --since-owner works on the other machine too
    assert H.list_history(work, "server-audit")[0]["label"] == "v0"
    assert "+2. check memory" in H.diff_skill(work, "server-audit", since="owner")


def test_export_without_history_carries_none(skills, tmp_path):
    H.snapshot_skill(skills, "server-audit", reason="r")
    archive = H.export_skill(skills, "server-audit", tmp_path / "x.tar.gz", with_history=False)
    assert H.import_skill(_other_machine(tmp_path), archive)["history_versions"] == 0


def test_import_will_not_overwrite_without_replace_and_snapshots_when_it_does(skills, tmp_path):
    archive = H.export_skill(skills, "server-audit", tmp_path / "a.tar.gz")
    work = _other_machine(tmp_path)
    H.import_skill(work, archive)
    edit(work, "LOCAL WORK\n")
    with pytest.raises(H.SkillHistoryError, match="already installed"):
        H.import_skill(work, archive)
    assert "LOCAL WORK" in (work / "server-audit" / "SKILL.md").read_text(encoding="utf-8")
    assert H.import_skill(work, archive, replace=True)["replaced"] is True
    assert "LOCAL WORK" not in (work / "server-audit" / "SKILL.md").read_text(encoding="utf-8")
    assert any("LOCAL WORK" in (p / "SKILL.md").read_text(encoding="utf-8")
               for p in (work / ".history" / "server-audit").iterdir() if p.is_dir())  # recoverable


def test_import_strips_code_unless_explicitly_allowed(skills, tmp_path):
    (skills / "server-audit" / "tool.py").write_text("import os\nos.system('echo pwned')\n", encoding="utf-8")
    (skills / "server-audit" / "schema.json").write_text("{}", encoding="utf-8")
    (skills / "server-audit" / "scripts").mkdir()
    (skills / "server-audit" / "scripts" / "go.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    archive = H.export_skill(skills, "server-audit", tmp_path / "c.tar.gz")

    safe = _other_machine(tmp_path)
    out = H.import_skill(safe, archive)
    assert set(out["stripped_code"]) == {"tool.py", "schema.json", "scripts/"}
    assert not (safe / "server-audit" / "tool.py").exists() and (safe / "server-audit" / "SKILL.md").is_file()

    trusting = tmp_path / "trusting"
    trusting.mkdir()
    assert H.import_skill(trusting, archive, allow_code=True)["stripped_code"] == []
    assert (trusting / "server-audit" / "tool.py").is_file()


def _tar(tmp_path, members, name="evil.tar.gz"):
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _file(name, data=b"x"):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


GOOD_SKILL = b"---\nname: evil\ndescription: d\n---\nbody\n"


@pytest.mark.parametrize("member", ["../outside.txt", "/abs/path.txt", "evil/../../up.txt"])
def test_import_refuses_traversal_and_absolute_paths(skills, tmp_path, member):
    archive = _tar(tmp_path, [_file("evil/SKILL.md", GOOD_SKILL), _file(member)])
    with pytest.raises(H.SkillHistoryError, match="unsafe path"):
        H.import_skill(skills, archive)
    assert not (tmp_path / "outside.txt").exists() and not (skills / "evil").exists()


def test_import_refuses_symlinks_and_special_files(skills, tmp_path):
    link = tarfile.TarInfo("evil/escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    archive = _tar(tmp_path, [_file("evil/SKILL.md", GOOD_SKILL), (link, None)])
    with pytest.raises(H.SkillHistoryError, match="link or special"):
        H.import_skill(skills, archive)
    assert not (skills / "evil").exists()


def test_import_refuses_bad_shapes(skills, tmp_path):
    with pytest.raises(H.SkillHistoryError, match="exactly one skill"):
        H.import_skill(skills, _tar(tmp_path, [_file("a/SKILL.md", GOOD_SKILL), _file("b/SKILL.md", GOOD_SKILL)], "two.tar.gz"))
    with pytest.raises(H.SkillHistoryError, match="no SKILL.md"):
        H.import_skill(skills, _tar(tmp_path, [_file("evil/readme.txt")], "none.tar.gz"))
    with pytest.raises(H.SkillHistoryError, match="no such archive"):
        H.import_skill(skills, tmp_path / "missing.tar.gz")
    many = _tar(tmp_path, [_file("evil/SKILL.md", GOOD_SKILL)] + [_file(f"evil/f{i}") for i in range(H.MAX_IMPORT_MEMBERS)], "many.tar.gz")
    with pytest.raises(H.SkillHistoryError, match="entries"):
        H.import_skill(skills, many)
    assert not (skills / "evil").exists()
