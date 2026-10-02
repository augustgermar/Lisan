"""Ratifying beliefs: the owner's ceremony, checked end to end.

Found by doing it for real: the artifact told the owner to "prune the lines you
reject" when the code reads candidates from the frontmatter (so pruning did
nothing and a subset could not be ratified at all), a formed belief was never
indexed (invisible to retrieval), and a candidate that failed re-verification was
dropped without a word.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lisan.frontmatter import dump_markdown, load_markdown
from lisan.tools import belief_formation as BF
from lisan.tools import learning as L
from lisan.tools import self_episodes as SE
from lisan.tools.db import connect
from lisan.tools.rebuild_index import ensure_index_schema


@pytest.fixture()
def world(tmp_path):
    vault = tmp_path / "vault"
    (vault / "self" / "episodes").mkdir(parents=True)
    (vault / "reports").mkdir()
    db = tmp_path / "idx.sqlite"
    conn = connect(db)
    ensure_index_schema(conn)
    conn.commit()
    conn.close()
    for skill in ("gmail_search", "research", "youtube_transcript"):
        conn = L._connect(db)
        for i, day in enumerate(["2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"]):
            conn.execute("INSERT INTO skill_usage (skill, skill_kind, event_id, used_at, outcome) VALUES (?,?,?,?,?)",
                         (skill, "instructional", f"turn:job.{skill}.{i}", f"{day}T10:00:00Z", "no_error_seen"))
        conn.commit()
        conn.close()
    SE.assemble_self_episodes(vault, db)
    artifact = Path(BF.run_belief_extraction(vault)["artifact"])
    return type("W", (), {"vault": vault, "db": db, "artifact": artifact, "tmp": tmp_path})


def beliefs(world):
    return sorted(p.name for p in (world.vault / "self" / "beliefs").glob("*.md")) if (world.vault / "self" / "beliefs").exists() else []


def fm(path):
    return dict(load_markdown(path).frontmatter)


# ── the artifact is an honest interface ─────────────────────────────────────

def test_the_artifact_numbers_its_candidates_and_says_how_to_choose(world):
    text = world.artifact.read_text(encoding="utf-8")
    assert "1. **My gmail_search skill has held up in use.**" in text
    assert "2. **My research skill has held up in use.**" in text and "3. **My youtube_transcript" in text
    assert "--only 1 3" in text
    assert "changes nothing" in text  # and it no longer pretends that editing the page does anything
    assert "Prune lines" not in text


def test_editing_the_visible_lines_really_does_change_nothing(world):
    """The statement the artifact makes about itself must be true."""
    doc = load_markdown(world.artifact)
    body = "\n".join(l for l in doc.body.splitlines() if "research" not in l and "youtube" not in l)
    world.artifact.write_text(dump_markdown(dict(doc.frontmatter), body), encoding="utf-8")
    formed = BF.ratify_beliefs(world.vault, artifact_path=world.artifact, db_path=world.db)
    assert len(formed) == 3  # all three, because the frontmatter decides


# ── ratifying ───────────────────────────────────────────────────────────────

def test_ratifying_forms_the_beliefs_with_evidence_provenance_and_a_capped_confidence(world):
    formed = BF.ratify_beliefs(world.vault, artifact_path=world.artifact, db_path=world.db)
    assert len(formed) == 3 and len(beliefs(world)) == 3
    belief = fm(next(p for p in formed if "gmail-search" in p.name))
    assert belief["summary"] == "My gmail_search skill has held up in use."
    assert belief["belief_confidence"] == "medium"  # never higher at birth; earned through reconcile
    assert belief["provenance"] == "formed" and belief["ratified_by"] == "owner" and belief["ratified_on"]
    assert belief["formed_from"].startswith("report.belief-extraction.")
    assert len(belief["evidence_refs"]) == 4 and all(r.startswith("self_episode.skill-gmail-search-") for r in belief["evidence_refs"])
    assert belief["revisions"] == []


def test_only_ratifies_exactly_the_numbers_chosen(world):
    formed = BF.ratify_beliefs(world.vault, artifact_path=world.artifact, only=[2], db_path=world.db)
    assert [fm(p)["summary"] for p in formed] == ["My research skill has held up in use."]
    assert len(beliefs(world)) == 1
    later = BF.ratify_beliefs(world.vault, artifact_path=world.artifact, only=[1, 3], db_path=world.db)  # the rest, later
    assert len(later) == 2 and len(beliefs(world)) == 3


@pytest.mark.parametrize("only,message", [([0], "No candidate number"), ([4], "it lists 1-3"), ([1, 9], "9"), ([], "selected no candidates")])
def test_a_bad_selection_is_refused_before_anything_is_formed(world, only, message):
    with pytest.raises(ValueError, match=message):
        BF.ratify_beliefs(world.vault, artifact_path=world.artifact, only=only, db_path=world.db)
    assert beliefs(world) == []


def test_a_formed_belief_is_indexed_so_retrieval_can_see_it(world):
    (path,) = BF.ratify_beliefs(world.vault, artifact_path=world.artifact, only=[1], db_path=world.db)
    conn = sqlite3.connect(world.db)
    row = conn.execute("SELECT type, path, summary FROM files WHERE path = ?", (f"self/beliefs/{path.name}",)).fetchone()
    assert row == ("self_belief", f"self/beliefs/{path.name}", "My gmail_search skill has held up in use.")


def test_ratifying_twice_forms_nothing_new_and_says_so(world):
    first, skipped_first = BF.ratify_beliefs_detailed(world.vault, artifact_path=world.artifact, db_path=world.db)
    again, skipped = BF.ratify_beliefs_detailed(world.vault, artifact_path=world.artifact, db_path=world.db)
    assert len(first) == 3 and skipped_first == [] and again == []
    assert len(skipped) == 3 and all("already formed" in reason for _, reason in skipped)
    assert len(beliefs(world)) == 3


# ── the evidence is re-verified, never taken on the artifact's word ─────────

def test_fabricated_evidence_is_refused_and_the_reason_is_given(world):
    doc = load_markdown(world.artifact)
    data = dict(doc.frontmatter)
    candidates = data["belief_extraction"]["candidates"]
    candidates[0]["supporting"] = [f"self_episode.invented-{i}" for i in range(5)]  # the owner edits the record, or it is tampered with
    world.artifact.write_text(dump_markdown(data, doc.body), encoding="utf-8")
    formed, skipped = BF.ratify_beliefs_detailed(world.vault, artifact_path=world.artifact, db_path=world.db)
    assert len(formed) == 2  # the two honest candidates still form
    (statement, reason), = skipped
    assert statement == "My gmail_search skill has held up in use."
    assert "only 0 of its 5 cited episode(s) exist" in reason and "needs 3 on 2" in reason


def test_evidence_that_exists_but_on_one_day_is_not_enough(world):
    doc = load_markdown(world.artifact)
    data = dict(doc.frontmatter)
    cand = data["belief_extraction"]["candidates"][0]
    one_day = [r for r in cand["supporting"]][:3]
    epis = world.vault / "self" / "episodes"
    # re-date the cited episodes to a single day: they still exist, but no longer span two
    for f in epis.glob("*gmail-search*.md"):
        text = f.read_text(encoding="utf-8")
        f.write_text(text.replace('"created": "2026-08-21"', '"created": "2026-08-20"').replace('"created": "2026-08-22"', '"created": "2026-08-20"')
                     .replace('"created": "2026-08-23"', '"created": "2026-08-20"'), encoding="utf-8")
    cand["supporting"] = one_day
    world.artifact.write_text(dump_markdown(data, doc.body), encoding="utf-8")
    formed, skipped = BF.ratify_beliefs_detailed(world.vault, artifact_path=world.artifact, only=[1], db_path=world.db)
    assert formed == [] and "on 1 day(s)" in skipped[0][1]


def test_an_artifact_with_no_candidates_is_an_error_not_a_silent_success(world, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text(dump_markdown({"id": "report.x", "type": "report", "artifact_kind": "beliefs", "belief_extraction": {"candidates": []}}, "# none\n"), encoding="utf-8")
    with pytest.raises(ValueError, match="nothing to ratify"):
        BF.ratify_beliefs(world.vault, artifact_path=empty, db_path=world.db)


# ── the command ─────────────────────────────────────────────────────────────

def run(args, capsys):
    from lisan.cli import main

    code = main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_command_ratifies_a_chosen_subset_and_reports_it(world, capsys):
    code, out, _ = run(["self", "ratify", "--vault", str(world.vault), "--db-path", str(world.db), "--from", str(world.artifact), "--only", "1", "3"], capsys)
    assert code == 0 and "✓ Ratified 2 belief(s)" in out
    assert out.count("✓ Formed belief:") == 2 and len(beliefs(world)) == 2


def test_the_command_explains_what_it_skipped(world, capsys):
    common = ["self", "ratify", "--vault", str(world.vault), "--db-path", str(world.db), "--from", str(world.artifact)]
    run(common, capsys)
    code, out, _ = run(common, capsys)
    assert code == 0 and "– Skipped: My gmail_search skill has held up in use." in out and "already formed" in out
    assert "✓ Ratified 0 belief(s)" in out


def test_the_command_refuses_a_bad_selection_and_a_provisional_belief(world, capsys):
    base = ["self", "ratify", "--vault", str(world.vault), "--db-path", str(world.db), "--from", str(world.artifact)]
    code, _, err = run(base + ["--only", "7"], capsys)
    assert code == 1 and "No candidate number(s) 7" in err
    code, _, err = run(base + ["--provisional"], capsys)
    assert code == 1 and "no provisional path" in err
    assert beliefs(world) == []


def _write_belief(vault, name, summary, status="active", conf="medium"):
    import json
    d = vault / "self" / "beliefs"
    d.mkdir(parents=True, exist_ok=True)
    fm = {"id": f"self_belief.{name}", "type": "self_belief", "status": status,
          "summary": summary, "belief_confidence": conf}
    (d / f"{name}.md").write_text(f"---\n{json.dumps(fm)}\n---\n\n# Belief\n\n{summary}\n", encoding="utf-8")


def test_relevant_self_beliefs_match_skill_name_and_self_queries(tmp_path):
    from lisan.tools.retrieval import _relevant_self_beliefs
    _write_belief(tmp_path, "a", "My gmail_search skill has held up in use.")
    _write_belief(tmp_path, "b", "My research skill has held up in use.", status="retired")
    _write_belief(tmp_path, "c", "My youtube_transcript skill fails often.", conf="low")
    got = _relevant_self_beliefs(tmp_path, "is gmail search reliable?")
    assert [i for _, _, i in got] == ["self_belief.a"]
    assert _relevant_self_beliefs(tmp_path, "what is the weather") == []
    ids = [i for _, _, i in _relevant_self_beliefs(tmp_path, "tell me about yourself")]
    assert ids == ["self_belief.a", "self_belief.c"]  # retired excluded, stronger first
