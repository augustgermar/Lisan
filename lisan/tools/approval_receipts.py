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
import time
from pathlib import Path
from typing import Any


class ReceiptError(ValueError):
    """A receipt is absent, invalid, expired, used, or mismatched."""


def receipt_root() -> Path:
    configured = os.environ.get("LISAN_RECEIPT_DIR")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "lisan" / "approval-receipts"


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
    root = receipt_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n")
    os.chmod(path, 0o600)


def action_records_path() -> Path:
    return receipt_root() / "action_records.jsonl"


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
    """Append a durable action record in the runtime-only receipt directory."""
    root = receipt_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = action_records_path()
    encoded = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
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
