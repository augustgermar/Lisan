"""Shared Google OAuth + Gmail REST client for Lisan gmail skills.

Pure standard library (``urllib``) — no third-party dependency, matching
Lisan's deterministic-first philosophy. Credentials are provisioned by the
user via the setup broker (`setup.py` in this directory); nothing here is
bundled with the repo.

Credential directory resolution order:
1. ``LISAN_GOOGLE_CREDENTIALS_DIR`` environment variable
2. ``skills.google.credentials_dir`` in config.json
3. ``~/.local/share/Lisan/credentials/google``

Files inside the credential directory:
- ``client_secret.json`` — OAuth client downloaded from Google Cloud Console
- ``token.json`` — authorized-user token minted by setup.py (mode 0600)
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Deliberately unreachable redirect: the browser shows an error page after
# consent and the user copies the full URL (which carries ?code=...) back to
# the agent. Must match exactly between the auth URL and the code exchange.
REDIRECT_URI = "http://localhost:1"

NOT_SET_UP = (
    "Google credentials are not set up. Walk the user through onboarding:\n"
    "1. Run: lisan skills setup gmail_search -- --check   (state + next step)\n"
    "2. The user creates an OAuth 'Desktop app' client in Google Cloud Console\n"
    "   (enable the Gmail API) and downloads client_secret.json.\n"
    "3. Run: lisan skills setup gmail_search -- --client-secret /path/to/client_secret.json\n"
    "4. Run: lisan skills setup gmail_search -- --auth-url   and send the user the URL.\n"
    "5. The user authorizes, then pastes back the full redirected URL (it will\n"
    "   look like an error page — that is expected).\n"
    "6. Run: lisan skills setup gmail_search -- --auth-code 'THE_PASTED_URL'\n"
)


class GoogleAuthError(RuntimeError):
    pass


def credentials_dir(config: dict[str, Any] | None = None) -> Path:
    env = os.environ.get("LISAN_GOOGLE_CREDENTIALS_DIR")
    if env:
        return Path(env).expanduser()
    if config:
        configured = (config.get("skills") or {}).get("google", {}).get("credentials_dir")
        if configured:
            return Path(str(configured)).expanduser()
    return Path.home() / ".local" / "share" / "Lisan" / "credentials" / "google"


def client_secret_path(config: dict[str, Any] | None = None) -> Path:
    return credentials_dir(config) / "client_secret.json"


def token_path(config: dict[str, Any] | None = None) -> Path:
    return credentials_dir(config) / "token.json"


def load_client_secret(config: dict[str, Any] | None = None) -> dict[str, Any]:
    path = client_secret_path(config)
    if not path.exists():
        raise GoogleAuthError(NOT_SET_UP)
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = data.get("installed") or data.get("web") or data
    if not payload.get("client_id") or not payload.get("client_secret"):
        raise GoogleAuthError(
            f"{path} does not look like a Google OAuth client file "
            "(missing client_id/client_secret)."
        )
    return payload


def load_token(config: dict[str, Any] | None = None) -> dict[str, Any]:
    path = token_path(config)
    if not path.exists():
        raise GoogleAuthError(NOT_SET_UP)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GoogleAuthError(f"could not parse {path}: {exc}") from exc


def save_token(token: dict[str, Any], config: dict[str, Any] | None = None) -> Path:
    """Atomic write with owner-only permissions."""
    path = token_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(token, handle, indent=2)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def token_expired(token: dict[str, Any], *, skew_seconds: int = 120) -> bool:
    expiry = _parse_expiry(token.get("expiry"))
    if expiry is None:
        return True
    return datetime.now(timezone.utc) + timedelta(seconds=skew_seconds) >= expiry


def _post_form(url: str, fields: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GoogleAuthError(f"Google token endpoint returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GoogleAuthError(f"could not reach Google token endpoint: {exc.reason}") from exc


def refresh_token(token: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    refresh = token.get("refresh_token")
    if not refresh:
        raise GoogleAuthError(
            "stored token has no refresh_token — re-run the auth flow "
            "(lisan skills setup gmail_search -- --auth-url)."
        )
    payload = _post_form(
        str(token.get("token_uri") or DEFAULT_TOKEN_URI),
        {
            "client_id": str(token.get("client_id") or ""),
            "client_secret": str(token.get("client_secret") or ""),
            "refresh_token": str(refresh),
            "grant_type": "refresh_token",
        },
    )
    token = dict(token)
    token["token"] = payload["access_token"]
    expires_in = int(payload.get("expires_in") or 3600)
    token["expiry"] = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat().replace("+00:00", "Z")
    save_token(token, config)
    return token


def access_token(config: dict[str, Any] | None = None) -> str:
    token = load_token(config)
    if token_expired(token):
        token = refresh_token(token, config)
    value = str(token.get("token") or "")
    if not value:
        raise GoogleAuthError("token file has no access token; re-run setup.")
    return value


def api_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if params:
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((key, str(v)) for v in value)
            elif value is not None:
                pairs.append((key, str(value)))
        url = f"{url}?{urllib.parse.urlencode(pairs)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {access_token(config)}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code in (401, 403):
            raise GoogleAuthError(
                f"Gmail API returned {exc.code} (auth problem): {detail}\n"
                "If this persists, re-run: lisan skills setup gmail_search -- --check"
            ) from exc
        raise GoogleAuthError(f"Gmail API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GoogleAuthError(f"could not reach the Gmail API: {exc.reason}") from exc


# ── Gmail helpers ────────────────────────────────────────────────────────────


def headers_dict(msg: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
        if h.get("name")
    }


def extract_body(msg: dict[str, Any]) -> str:
    """Best-effort plain-text body from a Gmail `format=full` payload."""
    import base64

    def decode(data: str) -> str:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    payload = msg.get("payload", {})
    if payload.get("body", {}).get("data"):
        return decode(payload["body"]["data"])
    parts = list(payload.get("parts") or [])
    # Walk nested multiparts breadth-first; prefer text/plain, fall back to html.
    queue = list(parts)
    html_fallback = ""
    while queue:
        part = queue.pop(0)
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data:
            return decode(data)
        if mime == "text/html" and data and not html_fallback:
            html_fallback = decode(data)
        queue.extend(part.get("parts") or [])
    return html_fallback


def message_summary(msg: dict[str, Any]) -> dict[str, Any]:
    headers = headers_dict(msg)
    return {
        "id": msg.get("id", ""),
        "threadId": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []),
    }
