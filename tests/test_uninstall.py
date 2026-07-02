from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisan.cli import main
from lisan.tools.uninstall import uninstall_installation


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.install_root = self.root / ".lisan"
        self.bin_dir = self.root / ".local" / "bin"
        self.home = self.root / "home"
        self.rc_file = self.home / ".zshrc"

        (self.install_root / "repo" / "lisan").mkdir(parents=True, exist_ok=True)
        (self.install_root / "venv" / "bin").mkdir(parents=True, exist_ok=True)
        (self.install_root / "venv" / "bin" / "python").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.install_root / "vault" / "episodes").mkdir(parents=True, exist_ok=True)
        (self.install_root / "vault" / "episodes" / "memory.md").write_text("memory", encoding="utf-8")
        (self.install_root / "config.yaml").write_text("config", encoding="utf-8")
        (self.install_root / "lisan.sqlite").write_text("sqlite", encoding="utf-8")
        (self.install_root / "embeddings.bin").write_text("embeddings", encoding="utf-8")
        (self.install_root / "backups").mkdir(parents=True, exist_ok=True)
        (self.install_root / "backups" / "archive.tar.gz").write_text("backup", encoding="utf-8")
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        (self.bin_dir / "lisan").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        self.home.mkdir(parents=True, exist_ok=True)
        self.rc_file.write_text(
            "# Added by Lisan installer\nexport PATH=\"%s:$PATH\"\nexport FOO=bar\n" % self.bin_dir,
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_uninstall_installation_keeps_vault_and_removes_install(self) -> None:
        with patch.dict(os.environ, {"HOME": str(self.home), "SHELL": "/bin/zsh"}, clear=False):
            result = uninstall_installation(install_root=self.install_root, bin_dir=self.bin_dir, keep_vault=True)

        self.assertFalse((self.install_root / "repo").exists())
        self.assertFalse((self.install_root / "venv").exists())
        self.assertFalse((self.install_root / "config.yaml").exists())
        self.assertFalse((self.install_root / "lisan.sqlite").exists())
        self.assertFalse((self.install_root / "embeddings.bin").exists())
        self.assertFalse((self.bin_dir / "lisan").exists())
        self.assertTrue((self.install_root / "vault" / "episodes" / "memory.md").exists())
        self.assertTrue((self.install_root / "backups" / "archive.tar.gz").exists())
        self.assertNotIn("export PATH", self.rc_file.read_text(encoding="utf-8"))
        self.assertTrue(result.kept_vault)
        self.assertFalse(result.vault_removed)
        self.assertIn(str(self.rc_file), result.removed_path_entries)

    def test_cli_uninstall_prompts_and_can_purge_vault(self) -> None:
        fake_result = SimpleNamespace(
            install_root=self.install_root,
            vault=self.install_root / "vault",
            removed_paths=["/tmp/example"],
            removed_path_entries=["/tmp/home/.zshrc"],
            kept_vault=False,
            vault_removed=True,
        )
        with patch("builtins.input", return_value="UNINSTALL") as prompt, patch("lisan.cli.uninstall_installation", return_value=fake_result) as uninstall:
            with patch.dict(os.environ, {"HOME": str(self.home), "SHELL": "/bin/zsh"}, clear=False):
                code = main(["uninstall", "--purge-vault"])

        self.assertEqual(code, 0)
        prompt.assert_called_once()
        uninstall.assert_called_once_with(install_root=None, bin_dir=None, keep_vault=False)

    def test_cli_uninstall_yes_skips_prompt(self) -> None:
        fake_result = SimpleNamespace(
            install_root=self.install_root,
            vault=self.install_root / "vault",
            removed_paths=["/tmp/example"],
            removed_path_entries=["/tmp/home/.zshrc"],
            kept_vault=True,
            vault_removed=False,
        )
        with patch("builtins.input") as prompt, patch("lisan.cli.uninstall_installation", return_value=fake_result) as uninstall:
            with patch.dict(os.environ, {"HOME": str(self.home), "SHELL": "/bin/zsh"}, clear=False):
                code = main(["uninstall", "--yes"])

        self.assertEqual(code, 0)
        prompt.assert_not_called()
        uninstall.assert_called_once_with(install_root=None, bin_dir=None, keep_vault=True)


if __name__ == "__main__":
    unittest.main()
