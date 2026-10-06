"""Single-use owner approval receipts for consequential tool actions.

Receipt issuance belongs to an owner-facing adapter, never to an LLM tool
handler. Receipts live outside the repository and vault so normal model
workspace writes cannot mint one. Execution consumes them atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import fcntl


class ReceiptError(ValueError):
    """A receipt is absent, invalid, expired, used, or mismatched."""


def receipt_root() -> Path:
    configured = os.environ.get("LISAN_RECEIPT_DIR")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "lisan" / "approval-receipts"


def audit_root() -> Path:
    configured = os.environ.get("LISAN_AUDIT_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Lisan" / "audit"
    else:
        state_root = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
        root = Path(state_root).expanduser() / "lisan" / "audit"
    # A configured override must still stay outside the code checkout and
    # vault; those are not durable audit boundaries and may be model-visible.
    try:
        from ..paths import repo_root, vault_root
        resolved = root.resolve()
        protected = (repo_root().resolve(), vault_root().resolve())
        if any(resolved == path or path in resolved.parents for path in protected):
            raise ValueError("audit directory must be outside the repository and vault")
    except ImportError:
        pass
    return root


def arguments_hash(arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"receipt arguments are not JSON-serializable: {exc}") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _receipt_path(receipt_id: str) -> Path:
    if not receipt_id or len(receipt_id) != 64 or any(ch not in "0123456789abcdef" for ch in receipt_id):
        raise ReceiptError("invalid receipt id")
    return receipt_root() / f"{receipt_id}.json"


def _audit(event: dict[str, Any]) -> None:
    root = audit_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def action_records_path() -> Path:
    return audit_root() / "action_records.jsonl"


def _audit_lock_path() -> Path:
    return audit_root() / ".append.lock"


def _rotate_action_records(path: Path) -> None:
    max_bytes = max(1024, int(os.environ.get("LISAN_AUDIT_MAX_BYTES", str(10 * 1024 * 1024))))
    keep = max(1, int(os.environ.get("LISAN_AUDIT_SEGMENTS", "10")))
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        if older.exists():
            os.replace(older, newer)
    os.replace(path, path.with_name(f"{path.name}.1"))


def _navigation_url(arguments: dict[str, Any], result: Any | None = None) -> str | None:
    if isinstance(result, dict):
        for key in ("final_url", "url"):
            if result.get(key):
                return str(result[key])
    for key in ("url", "navigation_url"):
        if arguments.get(key):
            return str(arguments[key])
    return None


def _action_record(event: dict[str, Any]) -> None:
    """Append a durable, rotated action record outside the model workspace."""
    root = audit_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = action_records_path()
    encoded = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    lock_fd = os.open(_audit_lock_path(), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.chmod(_audit_lock_path(), 0o600)
        with os.fdopen(lock_fd, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _rotate_action_records(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except BaseException:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def record_action_result(
    receipt_id: str, *, tool: str, action: str, target: str,
    arguments: dict[str, Any], result: Any, navigation_url: str | None = None,
    timestamp: float | None = None,
) -> None:
    """Record the result of an action whose receipt was consumed."""
    _receipt_path(str(receipt_id))
    at = float(time.time() if timestamp is None else timestamp)
    _action_record({
        "event": "result",
        "timestamp": at,
        "receipt_id": str(receipt_id),
        "tool": str(tool),
        "action": str(action),
        "target": str(target),
        "arguments": arguments,
        "navigation_url": navigation_url or _navigation_url(arguments, result),
        "result": result,
    })


def issue_receipt(
    *, tool: str, action: str, target: str, arguments: dict[str, Any],
    recipient: str | None = None, ttl_seconds: int = 60, now: float | None = None,
) -> str:
    """Issue a receipt from an owner-confirmation component.

    This function is not registered as a model tool. The owner-facing caller
    must obtain real confirmation before invoking it.
    """
    ttl = int(ttl_seconds)
    if not 1 <= ttl <= 300:
        raise ReceiptError("receipt expiry must be between 1 and 300 seconds")
    issued_at = float(time.time() if now is None else now)
    receipt_id = secrets.token_hex(32)
    payload = {
        "receipt_id": receipt_id, "tool": str(tool), "action": str(action),
        "target": str(target), "arguments_hash": arguments_hash(arguments),
        "recipient": None if recipient is None else str(recipient),
        "issued_at": issued_at, "expires_at": issued_at + ttl,
    }
    root = receipt_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = _receipt_path(receipt_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _audit({"event": "issued", "at": issued_at, **payload})
    _action_record({
        "event": "issued",
        "timestamp": issued_at,
        "receipt_id": receipt_id,
        "tool": str(tool),
        "action": str(action),
        "target": str(target),
        "arguments": arguments,
        "navigation_url": _navigation_url(arguments),
        "result": {"status": "receipt_issued"},
    })
    return receipt_id


def consume_receipt(
    receipt_id: str, *, tool: str, action: str, target: str,
    arguments: dict[str, Any], recipient: str | None = None, now: float | None = None,
) -> None:
    """Validate and atomically consume one exact receipt."""
    path = _receipt_path(str(receipt_id))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReceiptError("approval receipt is absent or already used") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError("approval receipt is unreadable") from exc

    current = float(time.time() if now is None else now)
    expected = {
        "tool": str(tool), "action": str(action), "target": str(target),
        "arguments_hash": arguments_hash(arguments),
        "recipient": None if recipient is None else str(recipient),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            _audit({"event": "rejected", "at": current, "receipt_id": str(receipt_id), "reason": f"mismatch:{key}"})
            raise ReceiptError(f"approval receipt does not match {key}")
    if current >= float(payload.get("expires_at", 0)):
        _audit({"event": "rejected", "at": current, "receipt_id": str(receipt_id), "reason": "expired"})
        raise ReceiptError("approval receipt has expired")

    used = path.with_name(f"{path.stem}.used.{secrets.token_hex(8)}")
    try:
        os.rename(path, used)
    except FileNotFoundError as exc:
        raise ReceiptError("approval receipt was already used") from exc
    try:
        _audit({"event": "consumed", "at": current, **payload})
        _action_record({
            "event": "consumed",
            "timestamp": current,
            "receipt_id": str(receipt_id),
            "tool": str(tool),
            "action": str(action),
            "target": str(target),
            "arguments": arguments,
            "navigation_url": _navigation_url(arguments),
            "result": {"status": "receipt_consumed"},
        })
    finally:
        used.unlink(missing_ok=True)
