"""Self-beliefs are revisited on a schedule, revisions earn confidence the
way formation does, and the prediction ledger reaches the reviser."""
from __future__ import annotations

from datetime import UTC

from lisan.tools import dreamer_ops, job_policy
from lisan.tools.job_policy import coalesce_key_for_job


def test_reconcile_has_its_own_job_and_is_not_swallowed_by_compress():
    from lisan.tools.jobs import JOB_TYPES

    assert "dreamer.reconcile_beliefs" in JOB_TYPES
    a = coalesce_key_for_job("dreamer.maintenance", {"vault": "/v", "task": "compress"})
    b = coalesce_key_for_job("dreamer.reconcile_beliefs", {"vault": "/v"})
    assert a != b


def test_reconcile_is_queued_weekly_when_beliefs_exist(tmp_path, monkeypatch):
    monkeypatch.setattr("lisan.tools.self_beliefs.list_self_beliefs", lambda v: [{"id": "b"}])
    meta = {"vault": str(tmp_path)}
    assert job_policy._should_queue_belief_reconcile(meta, tmp_path / "none.sqlite")
    from datetime import datetime

    monkeypatch.setattr(job_policy, "_last_successful_job_time",
                        lambda job_type, db, conn=None: datetime.now(UTC))
    assert not job_policy._should_queue_belief_reconcile(meta, tmp_path / "none.sqlite")


def test_no_beliefs_no_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr("lisan.tools.self_beliefs.list_self_beliefs", lambda v: [])
    assert not job_policy._should_queue_belief_reconcile({"vault": str(tmp_path)}, None)


def test_one_episode_cannot_raise_a_belief_to_high(tmp_path, monkeypatch):
    belief = tmp_path / "belief.md"
    belief.write_text("x", encoding="utf-8")
    episodes = tmp_path / "self" / "episodes"
    episodes.mkdir(parents=True)
    from lisan.frontmatter import write_markdown

    for i in range(3):
        write_markdown(episodes / f"e{i}.md", {"id": f"self_episode.e{i}"}, "x")
    monkeypatch.setattr("lisan.tools.self_beliefs.list_self_beliefs",
                        lambda v: [{"id": "belief.x", "path": str(belief)}])
    seen = []
    monkeypatch.setattr("lisan.tools.self_beliefs.revise_self_belief",
                        lambda path, **kw: seen.append(kw["new_confidence"]) or path)
    monkeypatch.setattr("lisan.tools.learning_notice.beliefs_revised", lambda *a, **k: None)
    dreamer_ops._apply_belief_revisions(tmp_path, {"revisions": [
        {"belief_id": "belief.x", "new_statement": "s", "new_confidence": "high",
         "evidence_refs": ["self_episode.e0"]},
        {"belief_id": "belief.x", "new_statement": "s", "new_confidence": "high",
         "evidence_refs": ["self_episode.e0", "self_episode.e1", "self_episode.e2"]},
    ]})
    assert seen == ["medium", "high"]


def test_calibration_reaches_the_reconcile_bundle(tmp_path):
    line = dreamer_ops._belief_calibration(
        {"prediction_calibration": {"hits": 1, "misses": 4, "unclear": 0, "standing": "poor"}})
    assert "misses=4" in line and "standing=poor" in line
