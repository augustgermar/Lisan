from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lisan.paths as paths


class DataRootContainmentTests(unittest.TestCase):
    """Mutable data paths must never resolve into the live install during a
    test run. On 2026-07-27 a full-suite run silently replaced a populated
    production embeddings.bin (971 vectors) with an empty stub, because
    sqlite_path() and embeddings_path() both defaulted to repo_root(). The
    same ambient resolution had already been logged as a deviation candidate
    for sqlite_path."""

    def test_data_root_honours_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LISAN_DATA_HOME": tmp}):
                self.assertEqual(paths.data_root(), Path(tmp))
                self.assertEqual(paths.sqlite_path(), Path(tmp) / "lisan.sqlite")
                self.assertEqual(paths.embeddings_path(), Path(tmp) / "embeddings.bin")

    def test_data_root_defaults_to_the_install(self):
        """Outside a test process the default is still the install — but this
        assertion has to opt out of the containment to observe it, which is the
        containment working."""
        env = {k: v for k, v in os.environ.items() if k != "LISAN_DATA_HOME"}
        env["LISAN_ALLOW_TEST_DATA_ROOT"] = "1"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.data_root(), paths.repo_root())

    def test_a_test_process_never_resolves_the_live_install(self):
        """The runner-independent guard. tests/__init__.py sets LISAN_DATA_HOME,
        but `python -m unittest discover -s tests` imports test modules
        top-level and never runs that init — measured 2026-07-29, under that
        runner data_root() resolved to the live install holding the production
        index and 971 embedding vectors. Deciding containment at the resolution
        site makes it hold for every runner, including ones not yet written."""
        env = {k: v for k, v in os.environ.items()
               if k not in {"LISAN_DATA_HOME", "LISAN_ALLOW_TEST_DATA_ROOT"}}
        with patch.dict(os.environ, env, clear=True):
            paths._TEST_DATA_ROOT = None
            try:
                with self.assertWarns(RuntimeWarning):
                    resolved = paths.data_root()
            finally:
                paths._TEST_DATA_ROOT = None
        self.assertNotEqual(resolved, paths.repo_root())
        self.assertTrue(resolved.exists())
        # Derived mutable paths follow the redirect, so nothing writes to the
        # real index by resolving one level down.
        with patch.dict(os.environ, env, clear=True):
            paths._TEST_DATA_ROOT = None
            try:
                self.assertNotIn(str(paths.repo_root()), str(paths.sqlite_path()))
                self.assertNotIn(str(paths.repo_root()), str(paths.embeddings_path()))
            finally:
                paths._TEST_DATA_ROOT = None

    def test_the_redirect_is_audible(self):
        """A redirect nobody is told about is the same failure mode as an
        embedder that skips and reports success."""
        env = {k: v for k, v in os.environ.items()
               if k not in {"LISAN_DATA_HOME", "LISAN_ALLOW_TEST_DATA_ROOT"}}
        with patch.dict(os.environ, env, clear=True):
            paths._TEST_DATA_ROOT = None
            try:
                with self.assertWarns(RuntimeWarning) as caught:
                    paths.data_root()
            finally:
                paths._TEST_DATA_ROOT = None
        message = str(caught.warning)
        self.assertIn("LISAN_DATA_HOME", message)
        self.assertIn("live install", message)

    def test_detector_does_not_rely_on_how_the_process_was_started(self):
        """Neither framework is imported by lisan's runtime, so their presence
        in sys.modules is the signal — not argv, not an env var a runner may or
        may not have set."""
        self.assertTrue(paths._looks_like_a_test_process())

    def test_resource_paths_are_not_redirected(self):
        """Read-only package resources must still come from the real package,
        or redirecting the data root would break prompt/schema loading."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LISAN_DATA_HOME": tmp}):
                self.assertEqual(paths.repo_root(), Path(paths.__file__).resolve().parents[1])
                self.assertNotIn(tmp, str(paths.schemas_dir()))
                self.assertTrue(str(paths.schemas_dir()).endswith("schemas"))


class EmptyIndexOverwriteTests(unittest.TestCase):
    """The runner-independent guard. LISAN_DATA_HOME (set in tests/__init__.py)
    protects pytest, but `unittest discover` does not import that package init
    — the same gap that let the outbound kill switch fail on 2026-07-26. So the
    real protection lives at the write itself, where it holds no matter who
    calls it: a test, a stray script, or a future agent rebuilding with a
    broken embedder."""

    def test_empty_index_refuses_to_replace_a_populated_one(self):
        from lisan.tools.vector_store import EmptyIndexOverwrite, write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", [0.1, 0.2]), ("b", [0.3, 0.4])], model="m", dimension=2)
            populated = path.read_text()

            with self.assertRaises(EmptyIndexOverwrite):
                write_embeddings(path, [("a", None), ("b", None)], model="none", dimension=0)

            self.assertEqual(path.read_text(), populated)  # untouched

    def test_empty_index_is_fine_when_there_is_nothing_to_lose(self):
        from lisan.tools.vector_store import write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", None)], model="none", dimension=0)  # fresh install
            self.assertTrue(path.exists())

    def test_populated_index_may_be_replaced_by_another_populated_one(self):
        from lisan.tools.vector_store import write_embeddings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.bin"
            write_embeddings(path, [("a", [0.1])], model="m", dimension=1)
            write_embeddings(path, [("a", [0.9]), ("b", [0.8])], model="m", dimension=1)
            self.assertIn("0.9", path.read_text())


if __name__ == "__main__":
    unittest.main()


class AmbientResolutionGuardTests(unittest.TestCase):
    """Ambient resolution is the bug *class* behind three separate incidents:
    escalation paging the owner's real phone from a test fixture, a routine test
    run replacing 971 production embedding vectors with an empty stub, and a
    logged sqlite_path deviation. Each was fixed at its own seam; the class
    survived because the containment lived in ``tests/__init__.py``, which one
    real runner never imports.

    Measured 2026-07-29 under ``python -m unittest discover -s tests``:
    ``LISAN_DATA_HOME`` unset, ``LISAN_NO_OUTBOUND`` unset, ``data_root()``
    resolving to the live install. The guards now ask what the process *is*,
    which is the one question every runner answers the same way.
    """

    def test_outbound_is_refused_in_a_test_process_without_the_env_switch(self):
        from lisan.tools import scheduler

        env = {k: v for k, v in os.environ.items()
               if k not in {"LISAN_NO_OUTBOUND", "LISAN_ALLOW_TEST_OUTBOUND"}}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                scheduler._deliver_owner_message("must never reach a phone")
        self.assertIn("test process", str(caught.exception))

    def test_the_resident_vault_check_alone_would_not_have_held(self):
        """Why the transport needs its own guard: escalation suppresses delivery
        for a foreign vault, but a test using the *ambient* vault is by
        definition the resident one, so it passes that check."""
        from lisan.tools.escalation import _is_resident_vault
        from lisan.paths import vault_root

        self.assertTrue(_is_resident_vault(vault_root()))

    def test_deliberate_opt_out_is_explicit(self):
        """The opt-out exists for tests exercising the paths below the guard,
        and it has to be set at the call site — never inherited ambiently."""
        from lisan.tools import scheduler

        env = {k: v for k, v in os.environ.items() if k != "LISAN_NO_OUTBOUND"}
        env["LISAN_ALLOW_TEST_OUTBOUND"] = "1"
        with patch.dict(os.environ, env, clear=True):
            with patch.object(scheduler, "load_config", return_value={}):
                with self.assertRaises(RuntimeError) as caught:
                    scheduler._deliver_owner_message("x")
        # Past the test-process guard, into the honest unconfigured error.
        self.assertIn("telegram is not configured", str(caught.exception))

    def test_validate_vault_checks_the_index_it_was_given(self):
        """`lisan validate --vault X` used to check vault X's aliases against
        whatever index the process defaulted to. Here the two happened to be the
        same install, so it passed by luck — the same ambient shape that let a
        test write into production."""
        import inspect
        from lisan.tools import validator

        self.assertIn("db_path", inspect.signature(validator.validate_vault).parameters)
        self.assertIn(
            "db_path",
            inspect.signature(validator._validate_alias_uniqueness).parameters,
        )
