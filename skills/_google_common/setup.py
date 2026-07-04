#!/usr/bin/env python3
"""Google OAuth2 onboarding broker for Lisan gmail skills.

Fully non-interactive — designed to be driven by the agent (or the user)
one command at a time, so it works over any conversation surface (CLI chat,
Telegram). Pure standard library; credentials are provisioned by the user
and stored under the Lisan credentials directory, never in the repo.

Commands (run via `lisan skills setup gmail_search -- <flag>` or directly):
  --check                        Is auth valid? Prints AUTHENTICATED / NOT_AUTHENTICATED
  --client-secret /path/to.json  Store the OAuth client file (Desktop app type)
  --auth-url                     Print the consent URL for the user to open
  --auth-code CODE_OR_URL        Exchange the pasted code (or full redirect URL)
  --revoke                       Revoke and delete the stored token

Agent workflow:
  1. --check. Exit 0 means ready — stop here.
  2. Ask the user to create a Google Cloud OAuth client (Desktop app) with the
     Gmail API enabled, and download client_secret.json. One-time, ~5 minutes:
       - https://console.cloud.google.com/apis/library  (enable Gmail API)
       - https://console.cloud.google.com/apis/credentials  (create OAuth client ID)
     If the app is in Testing, add the user's account as a test user.
  3. --client-secret PATH
  4. --auth-url  → send the URL to the user. After approving, the browser will
     land on an unreachable localhost page — that is expected. Ask the user to
     copy the ENTIRE address-bar URL.
  5. --auth-code "the pasted url"
  6. --check to confirm.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lisan_google import (  # noqa: E402
    DEFAULT_TOKEN_URI,
    REDIRECT_URI,
    SCOPES,
    GoogleAuthError,
    _post_form,
    client_secret_path,
    credentials_dir,
    load_client_secret,
    load_token,
    refresh_token,
    save_token,
    token_path,
)

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"


def extract_code(code_or_url: str) -> str:
    """Accept either a bare authorization code or the full redirect URL the
    user copied from the browser address bar."""
    value = code_or_url.strip().strip("'\"")
    if "://" in value or value.startswith("localhost"):
        parsed = urllib.parse.urlparse(value)
        code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0]
        if not code:
            raise GoogleAuthError(
                "no ?code= parameter found in that URL — make sure the entire "
                "address-bar URL was copied after approving access."
            )
        return code
    # Users sometimes paste "code=4/0Axxx&scope=..." fragments.
    if value.startswith("code="):
        return urllib.parse.parse_qs(value).get("code", [""])[0]
    return value


def cmd_check() -> int:
    if not client_secret_path().exists():
        print("NOT_AUTHENTICATED: no client_secret.json stored.")
        print(f"Next: --client-secret /path/to/client_secret.json  (dir: {credentials_dir()})")
        return 1
    if not token_path().exists():
        print("NOT_AUTHENTICATED: client secret is stored, but no token yet.")
        print("Next: --auth-url, have the user authorize, then --auth-code")
        return 1
    try:
        token = load_token()
        refresh_token(token)  # live probe: proves the refresh token still works
    except GoogleAuthError as exc:
        print(f"NOT_AUTHENTICATED: {exc}")
        return 1
    print("AUTHENTICATED")
    print(f"Scopes: {', '.join(load_token().get('scopes', []))}")
    return 0


def cmd_client_secret(path_arg: str) -> int:
    src = Path(path_arg).expanduser()
    if not src.exists():
        print(f"Error: {src} does not exist")
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error: {src} is not valid JSON: {exc}")
        return 1
    payload = data.get("installed") or data.get("web") or data
    if not payload.get("client_id") or not payload.get("client_secret"):
        print(f"Error: {src} is missing client_id/client_secret — download the "
              "OAuth client JSON for a 'Desktop app' credential.")
        return 1
    dest = client_secret_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(0o600)
    print(f"Stored client secret at {dest}")
    print("Next: --auth-url")
    return 0


def cmd_auth_url() -> int:
    client = load_client_secret()
    params = {
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    print(f"{AUTH_URI}?{urllib.parse.urlencode(params)}")
    print(
        "\nSend this URL to the user. After they approve, the browser will land "
        "on an unreachable localhost page (that's expected). Ask them to copy "
        "the ENTIRE address-bar URL and pass it to --auth-code.",
        file=sys.stderr,
    )
    return 0


def cmd_auth_code(code_or_url: str) -> int:
    client = load_client_secret()
    try:
        code = extract_code(code_or_url)
        payload = _post_form(
            str(client.get("token_uri") or DEFAULT_TOKEN_URI),
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
        )
    except GoogleAuthError as exc:
        print(f"Error: {exc}")
        return 1
    if not payload.get("refresh_token"):
        print(
            "Error: Google returned no refresh_token. Revoke the app's access at "
            "https://myaccount.google.com/permissions and redo --auth-url "
            "(the consent prompt must run fresh)."
        )
        return 1
    expires_in = int(payload.get("expires_in") or 3600)
    token = {
        "token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "token_uri": str(client.get("token_uri") or DEFAULT_TOKEN_URI),
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": str(payload.get("scope") or " ".join(SCOPES)).split(),
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=expires_in))
        .isoformat()
        .replace("+00:00", "Z"),
        "type": "authorized_user",
    }
    path = save_token(token)
    print(f"Token stored at {path}")
    print("Run --check to confirm.")
    return 0


def cmd_revoke() -> int:
    try:
        token = load_token()
    except GoogleAuthError:
        print("No stored token to revoke.")
        return 0
    for candidate in (token.get("refresh_token"), token.get("token")):
        if not candidate:
            continue
        try:
            _post_form(REVOKE_URI, {"token": str(candidate)})
            break
        except GoogleAuthError:
            continue
    token_file = token_path()
    if token_file.exists():
        token_file.unlink()
    print("Token revoked and deleted. Client secret left in place.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Google OAuth setup for Lisan gmail skills")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--client-secret", metavar="PATH")
    group.add_argument("--auth-url", action="store_true")
    group.add_argument("--auth-code", metavar="CODE_OR_URL")
    group.add_argument("--revoke", action="store_true")
    args = parser.parse_args()

    if args.check:
        return cmd_check()
    if args.client_secret:
        return cmd_client_secret(args.client_secret)
    if args.auth_url:
        return cmd_auth_url()
    if args.auth_code:
        return cmd_auth_code(args.auth_code)
    if args.revoke:
        return cmd_revoke()
    return 2


if __name__ == "__main__":
    sys.exit(main())
