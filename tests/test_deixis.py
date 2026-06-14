from __future__ import annotations

from pathlib import Path

import pytest

from lisan.tools.deixis import render_deixis, render_obj, render_for_display
from lisan.tools.primer_index import principal_aliases

CORE_TEXT = """---
principal:
  name: "August Morgan"
  aliases: ["August", "Gus"]
assistant:
  name: "Lisan"
  aliases: ["Lisan"]
deixis_frame: |
  I / me / Lisan = the assistant.
  you / your     = August, the principal.
  all other names = third parties; refer to them by name.
---

# Identity Core
"""


@pytest.fixture
def core_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "primer").mkdir(parents=True)
    (vault / "primer" / "identity-core.md").write_text(CORE_TEXT, encoding="utf-8")
    return vault


@pytest.fixture
def bare_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "bare"
    (vault / "primer").mkdir(parents=True)
    return vault


# --- interlocutor: conscious surface ({{principal}}->you, {{self}}->I) ------

def test_interlocutor_principal_token(core_vault: Path) -> None:
    assert render_deixis("{{principal}} decided to rest", "interlocutor", core_vault) == "you decided to rest"


def test_interlocutor_self_token(core_vault: Path) -> None:
    assert render_deixis("{{self}} suggested a walk", "interlocutor", core_vault) == "I suggested a walk"


def test_interlocutor_mixed_with_third_party(core_vault: Path) -> None:
    assert render_deixis("{{principal}} told Soren a story", "interlocutor", core_vault) == "you told Soren a story"


def test_interlocutor_both_tokens(core_vault: Path) -> None:
    assert render_deixis("{{self}} helped {{principal}}", "interlocutor", core_vault) == "I helped you"


def test_interlocutor_token_whitespace_tolerant(core_vault: Path) -> None:
    assert render_deixis("{{ principal }} and {{  self  }}", "interlocutor", core_vault) == "you and I"


def test_interlocutor_no_token_passthrough(core_vault: Path) -> None:
    text = "Soren and Dana went to the park."
    assert render_deixis(text, "interlocutor", core_vault) == text


def test_empty_string_returns_empty(core_vault: Path) -> None:
    assert render_deixis("", "interlocutor", core_vault) == ""


# --- substrate: writer/dreamer world-model ({{principal}}->the user) --------

def test_substrate_principal_token(core_vault: Path) -> None:
    assert render_deixis("{{principal}} decided to rest", "substrate", core_vault) == "the user decided to rest"


def test_substrate_self_token(core_vault: Path) -> None:
    assert render_deixis("{{self}} suggested a walk", "substrate", core_vault) == "Lisan suggested a walk"


def test_substrate_possessive(core_vault: Path) -> None:
    assert render_deixis("{{principal}}'s plan", "substrate", core_vault) == "the user's plan"


def test_substrate_mixed(core_vault: Path) -> None:
    assert render_deixis("{{self}} asked {{principal}}", "substrate", core_vault) == "Lisan asked the user"


# --- display: human view ({{principal}}->principal name) --------------------

def test_display_principal_token_resolves_name(core_vault: Path) -> None:
    assert render_deixis("{{principal}} decided to rest", "display", core_vault) == "August decided to rest"


def test_display_self_token(core_vault: Path) -> None:
    assert render_deixis("{{self}} suggested a walk", "display", core_vault) == "Lisan suggested a walk"


def test_display_possessive(core_vault: Path) -> None:
    assert render_deixis("{{principal}}'s park photo", "display", core_vault) == "August's park photo"


def test_display_fallback_no_core(bare_vault: Path) -> None:
    # No identity-core.md and no identity.md => no principal known => "the user".
    assert render_deixis("{{principal}} decided to rest", "display", bare_vault) == "the user decided to rest"


# --- back-compat: {{user}} is a legacy synonym for the canonical {{principal}}

def test_legacy_user_synonym_interlocutor(core_vault: Path) -> None:
    assert render_deixis("{{user}} decided to rest", "interlocutor", core_vault) == "you decided to rest"


def test_legacy_user_synonym_display(core_vault: Path) -> None:
    assert render_deixis("{{user}}'s park photo", "display", core_vault) == "August's park photo"


# --- render_obj: recursive structures --------------------------------------

def test_render_obj_dict(core_vault: Path) -> None:
    obj = {"story_thread": "{{principal}} is resting", "emotional_texture": "{{self}} stayed quiet"}
    assert render_obj(obj, "interlocutor", core_vault) == {
        "story_thread": "you is resting",
        "emotional_texture": "I stayed quiet",
    }


def test_render_obj_list(core_vault: Path) -> None:
    assert render_obj(["{{principal}} ran", "Dana walked"], "interlocutor", core_vault) == ["you ran", "Dana walked"]


def test_render_obj_passthrough_non_str(core_vault: Path) -> None:
    assert render_obj(5, "interlocutor", core_vault) == 5
    assert render_obj(None, "interlocutor", core_vault) is None
    assert render_obj(True, "interlocutor", core_vault) is True


def test_render_obj_nested_mixed(core_vault: Path) -> None:
    obj = {"open_threads": ["{{principal}} owes Soren a call"], "meta": {"turns": 3, "lead": "{{self}}"}}
    assert render_obj(obj, "substrate", core_vault) == {
        "open_threads": ["the user owes Soren a call"],
        "meta": {"turns": 3, "lead": "Lisan"},
    }


# --- the structured source-of-truth that display resolution depends on ------

def test_principal_aliases_from_core(core_vault: Path) -> None:
    assert principal_aliases(core_vault) == frozenset({"August", "Gus"})


def test_render_for_display_uses_principal_name(core_vault: Path) -> None:
    assert render_for_display("{{principal}} left.", core_vault) == "August left."


def test_render_for_display_fallback_no_core(bare_vault: Path) -> None:
    assert render_for_display("{{principal}} left.", bare_vault) == "the user left."
