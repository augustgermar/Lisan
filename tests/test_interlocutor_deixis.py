from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lisan.tools.memory_pipeline import _interlocutor_input
from lisan.tools.deixis import has_unresolved_token

_CORE = (
    '---\n'
    'principal:\n  name: "Mara Okonkwo-Reyes"\n  aliases: ["Mara"]\n'
    'assistant:\n  name: "Lisan"\n'
    'deixis_frame: |\n  frame\n'
    '---\n\n# Identity Core\n'
)


@pytest.fixture
def mara_vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "primer").mkdir(parents=True)
    (v / "primer" / "identity-core.md").write_text(_CORE, encoding="utf-8")
    return v


def _writer(**kw) -> dict:
    base = {
        "summary": "",
        "entities_to_create": [],
        "decisions_to_create": [],
        "open_loops_to_create": [],
        "significance": "medium",
        "questions": [],
    }
    base.update(kw)
    return base


def _state(**kw) -> SimpleNamespace:
    base = {
        "story_thread": "",
        "established": [],
        "open_threads": [],
        "emotional_texture": "",
        "turn_count": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_writer_summary_rendered_to_second_person() -> None:
    payload = _interlocutor_input(
        _writer(summary="{{principal}} captured a lovely moment at the park"),
        {"memory_type": "episode"},
        _state(),
    )
    assert payload["writer_summary"] == "you captured a lovely moment at the park"
    assert "{{" not in payload["writer_summary"]
    assert not has_unresolved_token(payload["writer_summary"])


def test_self_token_rendered_to_first_person() -> None:
    payload = _interlocutor_input(
        _writer(summary="{{self}} suggested a walk and {{principal}} agreed"),
        {"memory_type": "episode"},
        _state(),
    )
    assert payload["writer_summary"] == "I suggested a walk and you agreed"


def test_decision_and_open_loop_titles_rendered() -> None:
    payload = _interlocutor_input(
        _writer(
            decisions_to_create=[{"title": "{{principal}} will switch to gemini-2.5-pro"}],
            open_loops_to_create=[{"title": "{{principal}} to email Soren"}],
        ),
        {"memory_type": "episode"},
        _state(),
    )
    assert payload["decisions"] == ["you will switch to gemini-2.5-pro"]
    assert payload["open_loops"] == ["you to email Soren"]


def test_narrative_state_rendered() -> None:
    payload = _interlocutor_input(
        _writer(),
        {"memory_type": "episode", "score": 5, "reason": ["affect signal"]},
        _state(
            story_thread="{{principal}} is planning a trip",
            established=["{{principal}} met Dana"],
            open_threads=["{{principal}} owes Soren a call"],
            emotional_texture="{{self}} stayed warm",
            turn_count=3,
        ),
    )
    ns = payload["narrative_state"]
    assert ns["story_thread"] == "you is planning a trip"
    assert ns["established"] == ["you met Dana"]
    assert ns["open_threads"] == ["you owes Soren a call"]
    assert ns["emotional_texture"] == "I stayed warm"
    assert ns["turn_count"] == 3  # non-str values pass through untouched


def test_stale_emotional_texture_clears_on_neutral_turn() -> None:
    payload = _interlocutor_input(
        _writer(),
        {"memory_type": "episode", "score": 0, "reason": []},
        _state(emotional_texture="surge, empire energy"),
    )
    assert payload["narrative_state"]["emotional_texture"] == ""


def test_emotional_texture_persists_when_current_turn_has_affect() -> None:
    payload = _interlocutor_input(
        _writer(),
        {"memory_type": "episode", "score": 5, "reason": ["affect signal"]},
        _state(emotional_texture="surge, empire energy"),
    )
    assert payload["narrative_state"]["emotional_texture"] == "surge, empire energy"


def test_entities_kept_verbatim() -> None:
    payload = _interlocutor_input(
        _writer(entities_to_create=[{"name": "Priya"}, {"name": "Soren"}]),
        {"memory_type": "episode"},
        _state(),
    )
    # Third parties are genuine names — never tokenized, never rendered.
    assert payload["entities"] == ["Priya", "Soren"]


def test_user_correction_verbatim_and_state_omitted_on_correction() -> None:
    payload = _interlocutor_input(
        _writer(summary="{{principal}} did X"),
        {"memory_type": "correction"},
        _state(story_thread="{{principal}} believed Y"),
        user_text="No, it was Tuesday not Monday",
    )
    # Raw first-person user text is passed through verbatim...
    assert payload["user_correction"] == "No, it was Tuesday not Monday"
    # ...the stale narrative state is dropped...
    assert payload["narrative_state"] == {}
    # ...but the writer-authored summary is still rendered (not user-authored).
    assert payload["writer_summary"] == "you did X"


def test_priya_regression_no_name_no_token() -> None:
    # The 06-04 eval case: the interlocutor must never receive the principal's
    # name or a raw token in the summary. "Priya" survives only as a third-party
    # entity, never inside the summary prose.
    payload = _interlocutor_input(
        _writer(
            summary="{{principal}} captured a lovely moment at the park",
            entities_to_create=[{"name": "Priya"}],
        ),
        {"memory_type": "episode"},
        _state(),
    )
    assert payload["writer_summary"] == "you captured a lovely moment at the park"
    assert "Priya" not in payload["writer_summary"]
    assert "{{" not in payload["writer_summary"]
    assert not has_unresolved_token(payload["writer_summary"])
    assert "Priya" in payload["entities"]


# --- C1 regression: a Writer that emits the LITERAL principal name (not the
# token) must still not leak the name to the interlocutor, thanks to the
# deterministic tokenize-then-render backstop (vault-scoped). ------------------

def test_literal_principal_name_is_tokenized_then_rendered(mara_vault: Path) -> None:
    payload = _interlocutor_input(
        _writer(summary="Mara decided to confront Bram on Thursday"),
        {"memory_type": "episode"},
        _state(),
        vault=mara_vault,
    )
    assert payload["writer_summary"] == "you decided to confront Bram on Thursday"
    assert "Mara" not in payload["writer_summary"]


def test_literal_name_in_titles_is_tokenized(mara_vault: Path) -> None:
    payload = _interlocutor_input(
        _writer(decisions_to_create=[{"title": "Mara will email Bram"}],
                open_loops_to_create=[{"title": "Mara to call Priya"}]),
        {"memory_type": "episode"},
        _state(),
        vault=mara_vault,
    )
    assert payload["decisions"] == ["you will email Bram"]
    assert payload["open_loops"] == ["you to call Priya"]


def test_literal_name_in_narrative_state_is_tokenized(mara_vault: Path) -> None:
    payload = _interlocutor_input(
        _writer(),
        {"memory_type": "episode"},
        _state(story_thread="Mara is preparing for the Bram conversation",
               established=["Mara met Adaeze"]),
        vault=mara_vault,
    )
    assert payload["narrative_state"]["story_thread"] == "you is preparing for the Bram conversation"
    assert payload["narrative_state"]["established"] == ["you met Adaeze"]


def test_third_party_entities_still_verbatim_with_vault(mara_vault: Path) -> None:
    payload = _interlocutor_input(
        _writer(entities_to_create=[{"name": "Bram"}, {"name": "Priya"}]),
        {"memory_type": "episode"},
        _state(),
        vault=mara_vault,
    )
    assert payload["entities"] == ["Bram", "Priya"]
