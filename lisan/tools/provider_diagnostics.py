from __future__ import annotations

import getpass
import os
import pwd
import grp
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import load_config
from ..providers.base import LisanLLM, ProviderError
from ..providers.config import select_provider


@dataclass(slots=True)
class ProviderDiagnosticResult:
    provider: str
    model: str | None
    status: str
    error_type: str | None = None
    binary: str | None = None
    binary_path: str | None = None
    session_home: str | None = None
    session_path: str | None = None
    session_writable: bool = False
    minimal_completion: bool = False
    elapsed_ms: int | None = None
    errors: list[str] = field(default_factory=list)
    suggested_fixes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "error_type": self.error_type,
            "binary": self.binary,
            "binary_path": self.binary_path,
            "session_home": self.session_home,
            "session_path": self.session_path,
            "session_writable": self.session_writable,
            "minimal_completion": self.minimal_completion,
            "elapsed_ms": self.elapsed_ms,
            "errors": list(self.errors),
            "suggested_fixes": list(self.suggested_fixes),
            "details": self.details,
        }


def diagnose_provider(
    *,
    provider: str | None = None,
    model: str | None = None,
    config: dict[str, Any] | None = None,
    session_home: Path | None = None,
) -> ProviderDiagnosticResult:
    config = config or load_config()
    selection = select_provider(config, agent="elicitor", significance="medium", override_provider=provider, override_model=model)
    chosen_provider = selection.provider
    chosen_model = selection.model

    if chosen_provider == "codex":
        return _diagnose_codex(config=config, provider=chosen_provider, model=chosen_model, session_home=session_home)

    return _diagnose_generic(config=config, provider=chosen_provider, model=chosen_model)


def _diagnose_generic(*, config: dict[str, Any], provider: str, model: str | None) -> ProviderDiagnosticResult:
    errors: list[str] = []
    binary = None
    binary_path = None
    if provider in {"openai", "anthropic", "google", "local"}:
        details = {"base_url": config.get("providers", {}).get(provider, {}).get("base_url")}
    else:
        details = {}
    temp_dir = tempfile.TemporaryDirectory()
    try:
        temp_db = Path(temp_dir.name) / "diagnostics.sqlite"
        llm = LisanLLM(config=config, db_path=temp_db)
        start = _now_ms()
        llm.complete("Reply with OK.", provider=provider, model=model, agent="provider_check", significance="low")
        elapsed = _now_ms() - start
        return ProviderDiagnosticResult(
            provider=provider,
            model=model,
            status="ok",
            error_type=None,
            binary=binary,
            binary_path=binary_path,
            minimal_completion=True,
            elapsed_ms=elapsed,
            errors=errors,
            details=details,
        )
    except Exception as exc:
        errors.append(str(exc))
        return ProviderDiagnosticResult(
            provider=provider,
            model=model,
            status="failed",
            error_type=_classify_provider_error(exc),
            binary=binary,
            binary_path=binary_path,
            minimal_completion=False,
            elapsed_ms=None,
            errors=errors,
            suggested_fixes=_generic_suggestions(provider),
            details=details,
        )
    finally:
        temp_dir.cleanup()


def _diagnose_codex(*, config: dict[str, Any], provider: str, model: str | None, session_home: Path | None) -> ProviderDiagnosticResult:
    provider_cfg = config.get("providers", {}).get("codex", {})
    binary_env = str(provider_cfg.get("binary_env") or "CODEX_BIN")
    binary = os.environ.get(binary_env) or "codex"
    binary_path = shutil.which(binary)
    errors: list[str] = []
    fixes: list[str] = []
    session_home = Path(session_home or provider_cfg.get("home_dir") or os.environ.get("LISAN_CODEX_HOME") or Path.home())
    session_path = session_home / ".codex" / "sessions"
    details: dict[str, Any] = {
        "binary_env": binary_env,
        "session_owner": _owner_string(session_home),
        "session_mode": _mode_string(session_home),
    }
    if binary_path is None:
        errors.append(f"Missing coding agent binary: {binary}")
        fixes.append(f"Set {binary_env} to the coding agent executable path or install codex on PATH.")

    session_home_ok = _check_path_writable(session_home, errors, fixes, label="session home")
    session_path_ok = _check_path_writable(session_path, errors, fixes, label="session directory")
    minimal_completion = False
    elapsed_ms: int | None = None
    if binary_path is not None and session_home_ok and session_path_ok:
        temp_dir = tempfile.TemporaryDirectory()
        try:
            temp_db = Path(temp_dir.name) / "diagnostics.sqlite"
            diag_config = _codex_diag_config(config, session_home=session_home)
            llm = LisanLLM(config=diag_config, db_path=temp_db)
            start = _now_ms()
            llm.complete("Reply with OK.", provider=provider, model=model, agent="provider_check", significance="low")
            minimal_completion = True
            elapsed_ms = _now_ms() - start
        except Exception as exc:
            failure_type = _classify_provider_error(exc)
            if failure_type == "provider_auth_failure":
                errors.append("Coding agent auth is unavailable in the selected provider home.")
            errors.append(str(exc))
            details["failure_type"] = failure_type
            if failure_type == "provider_auth_failure":
                fixes[:0] = _codex_auth_fixes(session_home=session_home)
            elif failure_type == "session_permission_failure":
                fixes.extend(_codex_permission_fixes(session_home=session_home, session_path=session_path))
            else:
                fixes.extend(_codex_permission_fixes(session_home=session_home, session_path=session_path))
        finally:
            temp_dir.cleanup()

    status = "ok" if minimal_completion and not errors else "failed"
    if errors and status == "ok":
        status = "warning"
    if not minimal_completion:
        if details.get("failure_type") == "provider_auth_failure":
            fixes.extend(_codex_auth_fixes(session_home=session_home))
        elif details.get("failure_type") == "session_permission_failure":
            fixes.extend(_codex_permission_fixes(session_home=session_home, session_path=session_path))
        else:
            fixes.extend(_codex_permission_fixes(session_home=session_home, session_path=session_path))
    return ProviderDiagnosticResult(
        provider=provider,
        model=model,
        status=status,
        error_type=str(details.get("failure_type") or _classify_errors(errors)),
        binary=binary,
        binary_path=binary_path,
        session_home=str(session_home),
        session_path=str(session_path),
        session_writable=session_home_ok and session_path_ok,
        minimal_completion=minimal_completion,
        elapsed_ms=elapsed_ms,
        errors=_dedupe(errors),
        suggested_fixes=_dedupe(fixes),
        details=details,
    )


def _check_path_writable(path: Path, errors: list[str], fixes: list[str], *, label: str) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        errors.append(f"Unable to create {label} {path}: {exc}")
        return False
    probe = path / ".lisan_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception as exc:
        errors.append(f"Unable to write to {label} {path}: {exc}")
        return False


def _codex_diag_config(config: dict[str, Any], *, session_home: Path) -> dict[str, Any]:
    diag_config = dict(config)
    providers = dict(config.get("providers", {}))
    codex = dict(providers.get("codex", {}))
    codex["home_dir"] = str(session_home)
    providers["codex"] = codex
    diag_config["providers"] = providers
    return diag_config


def _codex_permission_fixes(*, session_home: Path, session_path: Path) -> list[str]:
    user = _current_user()
    group = _current_group()
    return [
        f"mkdir -p {session_path}",
        f"chmod 700 {session_home} {session_home / '.codex'} {session_path}",
        f"chown -R {user}:{group} {session_home / '.codex'}",
    ]


def _codex_auth_fixes(*, session_home: Path) -> list[str]:
    return [
        "Use --provider-auth shared so the coding agent uses the normal authenticated home directory.",
        f"Authenticate the coding agent in the isolated home if you intentionally want isolation: {session_home}",
        "Use --provider-auth mock for harness-only tests that do not need real authentication.",
    ]


def _generic_suggestions(provider: str) -> list[str]:
    return [
        f"Check provider configuration for {provider}.",
        "Verify the provider binary or API credentials are available.",
    ]


def _classify_provider_error(exc: Exception) -> str | None:
    text = f"{exc}".lower()
    if "401" in text or "missing bearer or basic authentication" in text or "unauthorized" in text:
        return "provider_auth_failure"
    if "permission denied" in text or "unable to write" in text or "is a directory" in text:
        return "session_permission_failure"
    if "missing codex binary" in text or "not found" in text:
        return "provider_binary_failure"
    return None


def _classify_errors(errors: list[str]) -> str | None:
    for error in errors:
        failure_type = _classify_provider_error(RuntimeError(error))
        if failure_type:
            return failure_type
    return None


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return getpass.getuser()


def _current_group() -> str:
    try:
        return grp.getgrgid(os.getgid()).gr_name
    except Exception:
        return "staff"


def _owner_string(path: Path) -> str:
    try:
        st = path.stat()
        return f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
    except Exception:
        return ""


def _mode_string(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        return oct(mode)
    except Exception:
        return ""


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
