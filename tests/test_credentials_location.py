"""Where secrets live, and what must never hold them.

Two directories in this system resist publication for different reasons, and
the difference is the whole point of these tests. The repo is protected by a
.gitignore rule — protection that has to keep working. The vault is protected
structurally, but it is the part designed to *travel*: backups copytree it into
an unencrypted tarball, purge deletes it, the wipe test clones it, and
LISAN_VAULT frequently points at a cloud-synced notes folder.

Credentials belong in neither. On 2026-08-13 the Google token sat in
repo/credentials/ (one `git add -f` from publication) and the live Telegram bot
token — which grants control of the running agent — sat in config.json, which
`backup.create_backup` copies into every archive. A copy was found on disk at
mode 644.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisan.paths import credentials_root


class _EnvIsolated(unittest.TestCase):
    """Never let these tests see, or write, the real credential store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in
                       ("LISAN_CREDENTIALS_DIR", "LISAN_HOME", "LISAN_TELEGRAM_TOKEN",
                        "LISAN_ALLOW_TEST_CREDENTIALS_ROOT")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class CredentialsRootTests(_EnvIsolated):
    def test_explicit_env_wins(self):
        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(credentials_root(), self.root / "elsewhere")

    def test_installer_home_is_used_when_set(self):
        os.environ["LISAN_ALLOW_TEST_CREDENTIALS_ROOT"] = "1"   # assert production order
        os.environ["LISAN_HOME"] = str(self.root / "install")
        self.assertEqual(credentials_root(), self.root / "install" / "credentials")

    def test_env_outranks_installer_home(self):
        os.environ["LISAN_HOME"] = str(self.root / "install")
        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "override")
        self.assertEqual(credentials_root(), self.root / "override")

    def test_a_test_process_cannot_reach_the_real_credential_store(self):
        """Containment at the seam, not in a fixture.

        With no explicit dir, a test asking for the credential store gets a
        throwaway. Three existing telegram tests began reading the owner's live
        bot token the moment it moved into this store; one asserted on what it
        found. `unittest discover` loads neither conftest.py nor
        tests/__init__.py, so this cannot be a fixture — it has to be decided
        here, where every runner passes through.
        """
        os.environ["LISAN_HOME"] = str(Path.home())          # would resolve to the real store
        resolved = credentials_root()
        self.assertIn("lisan-test-credentials-", str(resolved))
        self.assertNotEqual(resolved, Path.home() / ".lisan" / "credentials")
        self.assertNotEqual(resolved, Path.home() / "credentials")

    def test_never_resolves_inside_the_repo_or_the_vault(self):
        """The two places a credential must never land.

        The repo can be committed; the vault gets tarballed, purged, cloned and
        cloud-synced. Whatever the resolution order picks, it is a sibling of
        both.
        """
        from lisan.paths import repo_root, vault_root

        os.environ["LISAN_HOME"] = str(self.root / "install")
        resolved = credentials_root().resolve()
        for forbidden in (repo_root().resolve(), vault_root().resolve()):
            self.assertNotEqual(resolved, forbidden)
            self.assertNotIn(forbidden, resolved.parents)


class TelegramTokenStorageTests(_EnvIsolated):
    def _config(self, payload: dict) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_saving_moves_the_token_out_of_config_and_keeps_the_allowlist(self):
        from lisan.tools.telegram_bot import save_telegram_settings

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "creds")
        cfg_path = self._config({"telegram": {"token": "old-secret", "allowed_user_ids": [42]}})

        cred = save_telegram_settings("new-secret", [42, 99], path=cfg_path)

        self.assertEqual(json.loads(cred.read_text(encoding="utf-8"))["token"], "new-secret")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("token", cfg["telegram"])       # the whole point
        self.assertEqual(cfg["telegram"]["allowed_user_ids"], [42, 99])
        self.assertNotIn("new-secret", cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("old-secret", cfg_path.read_text(encoding="utf-8"))

    def test_credentials_file_is_owner_only(self):
        from lisan.tools.telegram_bot import save_telegram_settings

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "creds")
        cred = save_telegram_settings("s3cret", [42], path=self._config({}))
        mode = stat.S_IMODE(cred.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_resolution_precedence_env_then_file_then_config(self):
        from lisan.tools.telegram_bot import _resolve_settings, save_telegram_settings

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "creds")
        save_telegram_settings("from-file", [42], path=self._config({}))
        config = {"telegram": {"token": "from-config", "allowed_user_ids": [42]}}

        token, _ = _resolve_settings(config, include_env=False)
        self.assertEqual(token, "from-file")             # file beats config

        os.environ["LISAN_TELEGRAM_TOKEN"] = "from-env"
        token, _ = _resolve_settings(config, include_env=True)
        self.assertEqual(token, "from-env")              # env beats file

    def test_a_detached_service_sees_the_file_because_it_inherits_no_env(self):
        """include_env=False models the launchd/systemd view.

        The file must be readable in that mode — a token only reachable through
        an interactive shell's environment would leave the always-on service
        without one.
        """
        from lisan.tools.telegram_bot import _resolve_settings, save_telegram_settings

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "creds")
        save_telegram_settings("service-visible", [42], path=self._config({}))
        token, allowed = _resolve_settings({"telegram": {"allowed_user_ids": [42]}}, include_env=False)
        self.assertEqual(token, "service-visible")
        self.assertEqual(allowed, {42})

    def test_an_unmigrated_install_still_works_from_config(self):
        from lisan.tools.telegram_bot import _resolve_settings

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "empty")
        token, _ = _resolve_settings({"telegram": {"token": "legacy", "allowed_user_ids": [42]}},
                                     include_env=False)
        self.assertEqual(token, "legacy")

    def test_a_corrupt_credentials_file_falls_back_rather_than_killing_the_bot(self):
        """Losing the bot to a stray comma would be worse than the leak."""
        from lisan.tools.telegram_bot import _resolve_settings, telegram_credentials_path

        os.environ["LISAN_CREDENTIALS_DIR"] = str(self.root / "creds")
        path = telegram_credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json,,,", encoding="utf-8")

        token, _ = _resolve_settings({"telegram": {"token": "legacy", "allowed_user_ids": [42]}},
                                     include_env=False)
        self.assertEqual(token, "legacy")


class BackupExposureTests(unittest.TestCase):
    def test_backup_still_copies_config_so_config_must_stay_secret_free(self):
        """Pins the reason the token moved.

        `_write_tarball` copies config.json into every archive, and encryption
        is off by default. This test does not object to that — a restore wants
        the config — it pins the coupling, so that anyone who later puts a
        secret back into config.json has to read this and decide on purpose.
        """
        source = Path(__import__("lisan.tools.backup", fromlist=["backup"]).__file__).read_text(encoding="utf-8")
        self.assertIn("config.json", source)
        self.assertIn("encrypt_by_default", source)


if __name__ == "__main__":
    unittest.main()
