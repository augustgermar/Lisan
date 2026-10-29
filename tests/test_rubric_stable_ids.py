from __future__ import annotations

from lisan.tools import rubric


def _with_voice(monkeypatch, lines):
    monkeypatch.setattr(rubric, "kernel_voice_block", lambda vault: "\n".join(f"- {l}" for l in lines))


def test_a_reworded_invariant_keeps_its_dimension_id(tmp_path, monkeypatch):
    _with_voice(monkeypatch, ["Speaks plainly and warmly without hedging", "Admits uncertainty out loud"])
    first = {d["statement"]: d["id"] for d in rubric.rubric_from_kernel(tmp_path, persist=True)["dimensions"]}
    _with_voice(monkeypatch, ["Speaks warmly and plainly, never hedging", "Admits uncertainty out loud"])
    second = rubric.rubric_from_kernel(tmp_path, persist=True)["dimensions"]
    assert second[0]["id"] == first["Speaks plainly and warmly without hedging"]
    assert second[1]["id"] == first["Admits uncertainty out loud"]


def test_a_genuinely_new_invariant_gets_a_new_id(tmp_path, monkeypatch):
    _with_voice(monkeypatch, ["Admits uncertainty out loud"])
    rubric.rubric_from_kernel(tmp_path, persist=True)
    _with_voice(monkeypatch, ["Admits uncertainty out loud", "Uses dry humor sparingly"])
    ids = [d["id"] for d in rubric.rubric_from_kernel(tmp_path, persist=True)["dimensions"][:2]]
    assert len(set(ids)) == 2


def test_without_persist_the_registry_is_untouched(tmp_path, monkeypatch):
    _with_voice(monkeypatch, ["Admits uncertainty out loud"])
    rubric.rubric_from_kernel(tmp_path)
    assert not (tmp_path / rubric.REGISTRY_RELPATH).exists()


def test_same_kernel_same_rubric(tmp_path, monkeypatch):
    _with_voice(monkeypatch, ["Admits uncertainty out loud", "Speaks plainly"])
    assert rubric.rubric_from_kernel(tmp_path) == rubric.rubric_from_kernel(tmp_path)
