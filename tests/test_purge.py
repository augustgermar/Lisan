from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.cli import main
from lisan.config import load_config
from lisan.paths import ensure_repo_layout
from lisan.tools.purge import purge_installation


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = self.root / "lisan-vault"

        (self.vault / "episodes").mkdir(parents=True, exist_ok=True)
        (self.vault / "episodes" / "personal.md").write_text("personal", encoding="utf-8")
        (self.vault / "transcripts" / f"{date.today().isoformat()}.md").write_text("USER: secret", encoding="utf-8")
        (self.vault / "drafts").mkdir(parents=True, exist_ok=True)
        (self.vault / "drafts" / "draft.md").write_text("draft", encoding="utf-8")
        (self.root / "backups").mkdir(parents=True, exist_ok=True)
        (self.root / "backups" / "archive.tar.gz").write_text("backup", encoding="utf-8")
        (self.root / "lisan.sqlite").write_text("sqlite", encoding="utf-8")
        (self.root / "embeddings.bin").write_text("embeddings", encoding="utf-8")
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "local": {
                            "base_url": "http://localhost:11434/v1/chat/completions",
                            "default_model": "llama3.1",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_purge_installation_resets_vault_and_artifacts(self) -> None:
        result = purge_installation(base=self.root)

        self.assertFalse((self.vault / "episodes" / "personal.md").exists())
        self.assertFalse((self.vault / "transcripts" / f"{date.today().isoformat()}.md").exists())
        self.assertFalse((self.vault / "drafts" / "draft.md").exists())
        self.assertFalse((self.root / "lisan.sqlite").exists())
        self.assertFalse((self.root / "embeddings.bin").exists())
        self.assertTrue((self.root / "backups").exists())
        self.assertEqual(list((self.root / "backups").iterdir()), [])

        for rel in [
            "primer/identity.md",
            "primer/operating-style.md",
            "primer/high-stakes.yaml",
            "backup.md",
        ]:
            self.assertTrue((self.vault / rel).exists(), rel)

        config = load_config(self.root / "config.json")
        self.assertEqual(config["providers"]["local"]["base_url"], "http://127.0.0.1:8080/v1/chat/completions")
        self.assertIsNone(config["providers"]["local"]["default_model"])
        self.assertTrue(result.config_reset)
        self.assertTrue(result.seeded_files)

    def test_purge_installation_can_preserve_config_and_backup_before(self) -> None:
        backup_path = self.root / "backups" / "lisan-backup.tar.gz"
        with patch("lisan.tools.purge.create_backup", return_value=SimpleNamespace(archive_path=backup_path)) as backup:
            result = purge_installation(base=self.root, preserve_config=True, backup_before=True)

        self.assertTrue(backup.called)
        self.assertTrue(result.backup_created)
        self.assertEqual(result.backup_archive_path, str(backup_path))
        self.assertTrue((self.root / "config.json").exists())
        self.assertIn('"llama3.1"', (self.root / "config.json").read_text(encoding="utf-8"))
        self.assertFalse(result.config_reset)

    def test_purge_installation_uses_explicit_backup_destination(self) -> None:
        backup_dir = self.root / "custom-backups"
        backup_path = backup_dir / "lisan-backup.tar.gz"
        with patch("lisan.tools.purge.create_backup", return_value=SimpleNamespace(archive_path=backup_path)) as backup:
            result = purge_installation(base=self.root, preserve_config=True, backup_before=True, backup_destination=backup_dir)

        self.assertTrue(backup.called)
        args, kwargs = backup.call_args
        self.assertEqual(kwargs["destination"], backup_dir)
        self.assertEqual(result.backup_archive_path, str(backup_path))

    def test_purge_preserve_kernel_wipes_autobiography_not_self(self) -> None:
        """The Memory Wipe Test as an operation: episodes/entities go, the
        identity kernel survives byte-identical — amnesia, not a stranger."""
        kernel = self.vault / "primer" / "identity-core.md"
        kernel.parent.mkdir(parents=True, exist_ok=True)
        kernel_content = "---\nkernel_hash: \"abc\"\n---\n# Identity Core\nVega, dry wit.\n"
        kernel.write_text(kernel_content, encoding="utf-8")

        result = purge_installation(self.root, preserve_config=True, preserve_kernel=True)

        self.assertTrue(result.kernel_preserved)
        self.assertEqual(kernel.read_text(encoding="utf-8"), kernel_content)
        self.assertFalse((self.vault / "episodes" / "personal.md").exists())
        self.assertFalse((self.root / "lisan.sqlite").exists())

    def test_purge_honors_lisan_vault_env_when_no_explicit_base(self) -> None:
        """The wrong-vault footgun: with LISAN_VAULT set, purge must target
        the configured vault, not the default base/lisan-vault stub."""
        import os
        from unittest.mock import patch as _patch

        real_vault = self.root / "real-vault"
        (real_vault / "episodes").mkdir(parents=True, exist_ok=True)
        (real_vault / "episodes" / "e.md").write_text("x", encoding="utf-8")

        with _patch.dict(os.environ, {"LISAN_VAULT": str(real_vault)}), \
                _patch("lisan.tools.purge.repo_root", return_value=self.root):
            result = purge_installation(preserve_config=True)

        self.assertEqual(result.vault, real_vault)
        self.assertFalse((real_vault / "episodes" / "e.md").exists())

    def test_cli_purge_prompts_three_times_before_running(self) -> None:
        fake_result = SimpleNamespace(
            vault=self.vault,
            backup_created=False,
            backup_archive_path=None,
            removed_paths=["/tmp/example"],
            seeded_files=["primer/identity.md"],
        )
        with patch("builtins.input", side_effect=["PURGE", "n", "y"]) as prompt, patch("lisan.cli.purge_installation", return_value=fake_result) as purge:
            code = main(["purge"])

        self.assertEqual(code, 0)
        self.assertEqual(prompt.call_count, 3)
        purge.assert_called_once_with(preserve_config=False, preserve_kernel=False, backup_before=True, backup_destination=None)

    def test_cli_purge_yes_bypasses_prompts_for_automation(self) -> None:
        fake_result = SimpleNamespace(
            vault=self.vault,
            backup_created=True,
            backup_archive_path="/tmp/backup.tar.gz",
            removed_paths=["/tmp/example"],
            seeded_files=["primer/identity.md"],
        )
        with patch("builtins.input") as prompt, patch("lisan.cli.purge_installation", return_value=fake_result) as purge:
            code = main(["purge", "--yes", "--preserve-config", "--backup-before", "--backup-destination", str(self.root / "custom-backups")])

        self.assertEqual(code, 0)
        prompt.assert_not_called()
        purge.assert_called_once_with(
            preserve_config=True,
            preserve_kernel=False,
            backup_before=True,
            backup_destination=self.root / "custom-backups",
        )
