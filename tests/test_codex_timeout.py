"""A hung `codex exec` must fail loudly instead of blocking forever.

Chat turns, plan steps and the Adjutant all run through CodexClient, so the
limit lives in the provider and covers both the batch path and the live
progress path.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from lisan.providers.base import ProviderError
from lisan.providers.codex import DEFAULT_TIMEOUT_SECONDS, CodexClient, _resolve_timeout


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("LISAN_CODEX_TIMEOUT", raising=False)


def test_default_is_bounded():
    assert _resolve_timeout({}) == float(DEFAULT_TIMEOUT_SECONDS)
    assert DEFAULT_TIMEOUT_SECONDS > 0


def test_config_value_is_used():
    assert _resolve_timeout({"timeout_seconds": 90}) == 90.0


def test_env_beats_config(monkeypatch):
    monkeypatch.setenv("LISAN_CODEX_TIMEOUT", "5")
    assert _resolve_timeout({"timeout_seconds": 90}) == 5.0


def test_zero_disables_explicitly():
    assert _resolve_timeout({"timeout_seconds": 0}) is None


@pytest.mark.parametrize("bad", ["soon", -3, "", None])
def test_malformed_values_fall_back_to_the_default_never_to_unbounded(bad):
    assert _resolve_timeout({"timeout_seconds": bad}) == float(DEFAULT_TIMEOUT_SECONDS)


def test_batch_path_passes_the_limit_to_subprocess():
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        proc = MagicMock(returncode=0, stdout="ok", stderr="")
        return proc

    client = CodexClient({"providers": {"codex": {"timeout_seconds": 42}}})
    with patch("lisan.providers.codex._run_batch", side_effect=fake_run):
        client.complete("hi", agent="codex")
    assert seen["timeout"] == 42.0


def _sleepers():
    import subprocess

    out = subprocess.run(["pgrep", "-f", "sleep 61"], capture_output=True, text=True).stdout
    return [line for line in out.split() if line]


def _fake_codex(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.sh"
    script.write_text("#!/bin/sh\ncat >/dev/null\nsleep 61\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_BIN", str(script))
    return {"providers": {"codex": {"binary_env": "FAKE_CODEX_BIN", "timeout_seconds": 1}}}


def test_hung_process_is_killed_on_the_batch_path(tmp_path, monkeypatch):
    config = _fake_codex(tmp_path, monkeypatch)
    started = time.monotonic()
    with patch("lisan.providers.codex.progress_listener_active", return_value=False):
        with pytest.raises(ProviderError, match="timed out after 1s"):
            CodexClient(config).complete("hi", agent="codex", working_directory=tmp_path)
    assert time.monotonic() - started < 20


def test_hung_process_is_killed_on_the_progress_path(tmp_path, monkeypatch):
    config = _fake_codex(tmp_path, monkeypatch)
    started = time.monotonic()
    with (
        patch("lisan.providers.codex.progress_listener_active", return_value=True),
        patch("lisan.providers.codex.record_codex_progress"),
    ):
        with pytest.raises(ProviderError, match="timed out after 1s"):
            CodexClient(config).complete("hi", agent="codex", working_directory=tmp_path)
    assert time.monotonic() - started < 20


@pytest.mark.parametrize("progress", [False, True])
def test_timeout_kills_the_whole_process_group_not_just_the_child(tmp_path, monkeypatch, progress):
    """codex runs shell commands; a hung one must not outlive the timeout."""
    before = set(_sleepers())
    config = _fake_codex(tmp_path, monkeypatch)
    with (
        patch("lisan.providers.codex.progress_listener_active", return_value=progress),
        patch("lisan.providers.codex.record_codex_progress"),
    ):
        with pytest.raises(ProviderError):
            CodexClient(config).complete("hi", agent="codex", working_directory=tmp_path)
    time.sleep(0.3)
    assert set(_sleepers()) - before == set()
