from __future__ import annotations

from pathlib import Path

import pytest

from lisan.tools.record_factory import normalize_claim_owner

CORE_TEXT = """---
principal:
  name: "Alex Morgan"
  aliases: ["Alex", "Lex"]
assistant:
  name: "Lisan"
  aliases: ["Lisan"]
deixis_frame: |
  frame
---

# Identity Core
"""


@pytest.fixture
def core_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # normalize_claim_owner reads paths.vault_root(), which honors LISAN_VAULT.
    vault = tmp_path / "vault"
    (vault / "primer").mkdir(parents=True)
    (vault / "primer" / "identity-core.md").write_text(CORE_TEXT, encoding="utf-8")
    monkeypatch.setenv("LISAN_VAULT", str(vault))
    return vault


def test_principal_alias_resolves_to_user(core_vault: Path) -> None:
    assert normalize_claim_owner("Alex") == "user"
    assert normalize_claim_owner("Lex") == "user"


def test_non_principal_known_name_is_external_actor(core_vault: Path) -> None:
    # The latent bug: a third party in the cast used to be stamped owner="user".
    assert normalize_claim_owner("Wren") == "external_actor"
    assert normalize_claim_owner("Dana") == "external_actor"


def test_pronoun_and_agent_aliases_still_resolve(core_vault: Path) -> None:
    assert normalize_claim_owner("me") == "user"
    assert normalize_claim_owner("myself") == "user"
    assert normalize_claim_owner("i") == "user"
    assert normalize_claim_owner("Lisan") == "agent"
    assert normalize_claim_owner("writer") == "agent"
