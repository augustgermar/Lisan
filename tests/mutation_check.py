"""A small mutation check for the learning loop. Not collected by pytest.

    python tests/mutation_check.py [substring-filter]

For each entry it breaks one property in the source (one targeted edit), runs
the tests that are meant to defend that property, and reports CAUGHT if they
fail or MISSED if they still pass. A MISSED line is a test that does not test
what it claims. The source file is always restored.

Why this exists: green tests prove little until you have watched them fail. It
found a test whose dry-run step never reached the code it was guarding, and a
description-rewrite hole the gate's own tests had blessed.

Python's bytecode cache is keyed on (mtime in whole seconds, size), so two
back-to-back edits of one file could reuse a stale compile and give a false
verdict in either direction; this deletes the cache around every run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE, APPLY, REVIEW = "lisan/tools/skill_gate.py", "lisan/tools/skill_apply.py", "lisan/tools/skill_review.py"
LIFE, LEARN = "lisan/tools/skill_lifecycle.py", "lisan/tools/learning.py"
TG, TA, TR, TL = "tests/test_skill_gate.py", "tests/test_skill_apply.py", "tests/test_skill_review.py", "tests/test_skill_lifecycle.py"

# (label, file, exact text to find, replacement, test file, -k expression)
MUTANTS = [
    # ── the gate ──
    ("evidence not checked against the batch", GATE, "        if event_id not in batch_ids or event_id not in events:", "        if False:", TG, "evidence"),
    ("create needs only one event", GATE, 'MIN_EVIDENCE = {"patch": 1, "add_reference": 1, "create": 2}', 'MIN_EVIDENCE = {"patch": 1, "add_reference": 1, "create": 1}', TG, "two_distinct"),
    ("rehearsal history accepted as evidence", GATE, '        elif is_eval_conversation(events[event_id].get("conversation_id")):', "        elif False:", TG, "rehearsal"),
    ("pinned skills editable", GATE, "        if is_pinned(skills_dir, op.skill):", "        if False:", TG, "pinned"),
    ("privilege fields can be widened", GATE, "        if old.get(key) != new.get(key):", "        if False:", TG, "privileges"),
    ("negative claims allowed", GATE, "    claim = _first_negative_claim(added)\n    if claim:", "    claim = None\n    if claim:", TG, "negative_capability"),
    ("conditionals treated as claims", GATE, "            if match and not _CONDITIONAL.search(sentence[: match.start()]):", "            if match:", TG, "conditional_guidance"),
    ("injection text allowed", GATE, "    for pattern in _INJECTION:\n        if pattern.search(added):", "    for pattern in ():\n        if pattern.search(added):", TG, "orders"),
    ("credentials allowed", GATE, "    if mask_secrets_strict(added) != added:", "    if False:", TG, "credentials"),
    ("incident names allowed", GATE, '    if _BAD_NAME_PARTS.search(name or ""):', "    if False:", TG, "moment_a_ticket"),
    ("ambiguous old_text accepted", GATE, "            if hits != 1:", "            if hits < 1:", TG, "exactly_once"),
    ("two proposals to one skill both accepted", GATE, "        if verdict.accepted and op.skill in touched:", "        if False:", TG, "combined"),
    ("stale environment lessons allowed", GATE, "    if not evidence or not _ENVIRONMENT_VOCABULARY.search(added):\n        return []", "    return []", TG, "environment_lesson or fresh_event"),
    ("retired tool names allowed", GATE, '        if re.search(rf"\\b{re.escape(legacy)}\\b", added):', "        if False:", TG, "current_tool_names"),
    ("descriptions can be rewritten", GATE, '        elif old_description and old_description.rstrip(" .!;:") not in description:', "        elif False:", TG, "owners_wording"),
    ("negative-claim refusals not revisable", GATE, '    "credential", "give the agent orders", "is pinned",', '    "credential", "give the agent orders", "negative capability claim", "is pinned",', TR, "sent_back_once_to_be_reworded"),
    # ── the reviewer ──
    ("provider failure becomes 'nothing found'", REVIEW, 'provider_error_mode="raise", parse_error_mode="raise",', 'provider_error_mode="fallback", parse_error_mode="fallback",', TR, "raise_mode"),
    ("events marked reviewed on a dry run", REVIEW, "    if not dry_run:\n        learning.mark_reviewed(", "    if True:\n        learning.mark_reviewed(", TR, "dry_run_judges"),
    ("review never marks events reviewed", REVIEW, "    if not dry_run:\n        learning.mark_reviewed(", "    if False:\n        learning.mark_reviewed(", TR, "shadow_review_gates"),
    ("observe mode reviews anyway", REVIEW, '    if not dry_run and mode not in ("shadow", "auto"):', "    if False:", TR, "observe_mode_does_not_review"),
    ("trigger ignores the threshold", LEARN, "        if len(unreviewed_ids(db_path)) < review_every(config):", "        if False:", TR, "burst_of_events"),
    ("shadow mode applies", REVIEW, '    if mode == "auto" and not dry_run:\n        result.lifecycle', '    if mode in ("auto", "shadow") and not dry_run:\n        result.lifecycle', TR, "shadow_and_dry_run_never_apply"),
    ("dry run applies in auto", REVIEW, '    if mode == "auto" and not dry_run:\n        result.lifecycle', '    if mode == "auto":\n        result.lifecycle', TR, "shadow_and_dry_run_never_apply"),
    ("no cap on changes per review", REVIEW, "        if done >= cap:", "        if False:", TR, "cap_bounds"),
    ("manual apply skips the re-gate", REVIEW, "    for verdict in verdicts:\n        if not verdict.accepted:\n            continue\n        try:\n            verdict.applied = apply_change(\n                verdict.change, skills_dir=skills_dir, review_id=review_id, actor=\"owner\",", "    for verdict in verdicts:\n        if not verdict.accepted and False:\n            continue\n        try:\n            verdict.applied = apply_change(\n                verdict.change, skills_dir=skills_dir, review_id=review_id, actor=\"owner\",", TR, "stale_shadow_proposal"),
    # ── history ──
    ("a rollback overwrites a snapshot's label", "lisan/tools/skill_history.py", '              if e.get("version_id") and e.get("action") == "snapshot"}', '              if e.get("version_id")}', "tests/test_skill_history.py", "v0_survives or logs_written_before_the_fix"),
    # ── the applier ──
    ("no snapshot before applying", APPLY, "        if not change.is_new:\n            try:\n                snapshot = snapshot_skill(", "        if False:\n            try:\n                snapshot = snapshot_skill(", TA, "owners_text_as_v0"),
    ("stale plans are merged anyway", APPLY, "                if current != planned_against:", "                if False:", TA, "since_changed"),
    ("a late pin is ignored", APPLY, "            if is_pinned(skills_dir, change.skill):\n                raise ApplyRefused(", "            if False:\n                raise ApplyRefused(", TA, "pin_placed"),
    ("invalid results are kept", APPLY, "                problem = _valid(target)\n                if problem:", "                problem = None\n                if problem:", TA, "not_a_valid_skill_is_undone"),
    ("failed applies are not undone", APPLY, "            elif snapshot:\n                try:\n                    rollback_skill(", "            elif False:\n                try:\n                    rollback_skill(", TA, "midway or not_a_valid_skill"),
    ("no lock between writers", APPLY, "        fcntl.flock(self._handle, fcntl.LOCK_EX)", "        pass", TA, "serialise"),
    ("apply is not logged", APPLY, '        _log(skills_dir, {\n            "skill": change.skill, "action": "apply"', '        (lambda *a, **k: None)(skills_dir, {\n            "skill": change.skill, "action": "apply"', TA, "logged_with_its_evidence"),
    # ── migrations ──
    ("a lost column race crashes", "lisan/tools/db.py", '            if "duplicate column" in message:\n                return False', '            if False:\n                return False', "tests/test_db_migrations.py", "losing_the_race or four_processes"),
    ("a locked database is not waited out", "lisan/tools/db.py", '            if ("locked" in message or "busy" in message) and attempt < attempts - 1:', '            if False:', "tests/test_db_migrations.py", "waited_out"),
    # ── isolation ──
    ("tests can read the live config", "lisan/paths.py", '    if base is None and _looks_like_a_test_process() and os.environ.get("LISAN_ALLOW_TEST_CONFIG") != "1":', "    if False:", "tests/test_paths.py", "ConfigContainment"),
    # ── probation ──
    ("owner skills get promoted", LIFE, '        if prov["origin"] != "agent" or prov.get("pinned"):\n            continue', "        if False:\n            continue", TL, "only_the_loops_own"),
    ("one failure flags a skill", LIFE, "FLAG_AFTER_CONSECUTIVE_FAILURES = 2", "FLAG_AFTER_CONSECUTIVE_FAILURES = 1", TL, "decide"),
    ("promotion needs no age", LIFE, "(age is None or age >= PROMOTE_MIN_DAYS)", "True", TL, "decide"),
    ("agent is not told a skill is provisional", LIFE, '    if prov.get("status") == "provisional":\n        return (', "    if False:\n        return (", TL, "told_a_skills_standing"),
]


def main(argv: list[str]) -> int:
    only = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    missed = 0
    for label, rel, old, new, test, k in MUTANTS:
        if only and only not in label:
            continue
        path = ROOT / rel
        source = path.read_text(encoding="utf-8")
        if old not in source:
            print(f"!! cannot apply (the code moved): {label}")
            missed += 1
            continue
        path.write_text(source.replace(old, new, 1), encoding="utf-8")
        for stale in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
            stale.unlink()
        try:
            run = subprocess.run(
                [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:logging", test, "-k", k, "-x"],
                cwd=ROOT, capture_output=True, text=True, timeout=300, env=env,
            )
            caught = run.returncode != 0
            tail = (run.stdout.strip().splitlines() or [run.stderr[-200:]])[-1]
        finally:
            path.write_text(source, encoding="utf-8")
            for stale in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
                stale.unlink()
        print(("CAUGHT  " if caught else "MISSED  ") + label + "  -> " + tail)
        missed += 0 if caught else 1
    print(f"\n{len(MUTANTS) if not only else 'selected'} mutants; {missed} missed or unapplied")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
