"""The agent's own browser: headful, persistent, shared with the owner.

Design (chosen after the owner's attempts with bundled Playwright browsers
and MCP controllers fought persistence and shared control): the automation
does NOT own the browser. A real Google Chrome runs as its own desktop app
with a dedicated profile (``~/.lisan/browser-profile``) and a CDP debug
port; the agent *connects* per operation and detaches. Consequences, all
intended:

- The window is a first-class citizen of the owner's desktop. The owner
  can take the mouse anytime — log into something, solve a CAPTCHA, show
  the agent a page — and the agent inherits the session state.
- Persistence is Chrome's own: cookies, saved passwords, sessions, cache
  live in the profile directory and survive reboots. ``--restore-last-
  session`` brings the tabs back.
- Nothing breaks when our processes restart: the browser outlives them,
  and if the browser is closed, the next operation relaunches it.
- It is fully separate from the owner's personal browser (Brave).

Operations are deliberately small verbs (goto/read/click/type/screenshot/
tabs) — the conversation agent composes them, and the owner watches it
happen on screen.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from .log import log_error

CDP_PORT = 18223
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def profile_dir() -> Path:
    from ..paths import vault_root

    # sibling of the vault, inside the install — never inside the repo
    return vault_root().parent / "browser-profile"


def chrome_args() -> list[str]:
    return [
        CHROME,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
    ]


def _cdp_alive(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser(wait_seconds: float = 15.0) -> bool:
    """The browser is running with its debug port up, launching it if
    needed. Launched detached: it outlives every lisan process."""
    if _cdp_alive():
        return True
    profile_dir().mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            chrome_args(),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log_error(None, "browser launch failed", exc)
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _cdp_alive():
            return True
        time.sleep(0.4)
    return False


def browser_action(action: str, **kw: Any) -> dict[str, Any]:
    """One browser operation: connect over CDP, act, detach. The browser
    itself keeps running (and keeps the owner's hands on it)."""
    action = str(action or "").strip().lower()
    if action == "open":
        ok = ensure_browser()
        return {"ok": ok, "note": "browser is on screen" if ok else "could not launch Chrome"}
    if not ensure_browser():
        return {"ok": False, "error": "browser could not be started"}

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
        pages = [p for p in context.pages if not p.url.startswith("devtools")]
        page = pages[-1] if pages else context.new_page()

        if action == "goto":
            url = str(kw.get("url") or "").strip()
            if not url:
                return {"ok": False, "error": "goto needs a url"}
            if "://" not in url:
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"ok": True, "url": page.url, "title": page.title()}

        if action == "read":
            body = page.inner_text("body", timeout=10000)
            body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
            limit = int(kw.get("max_chars") or 6000)
            return {"ok": True, "url": page.url, "title": page.title(),
                    "text": body[:limit], "truncated": len(body) > limit}

        if action == "click":
            target = str(kw.get("target") or "").strip()
            if not target:
                return {"ok": False, "error": "click needs a target (visible text or CSS selector)"}
            try:
                page.get_by_text(target, exact=False).first.click(timeout=6000)
            except Exception:
                page.click(target, timeout=6000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            return {"ok": True, "url": page.url, "title": page.title()}

        if action == "type":
            target = str(kw.get("target") or "").strip()
            text = str(kw.get("text") or "")
            if not target:
                return {"ok": False, "error": "type needs a target selector or placeholder text"}
            try:
                loc = page.get_by_placeholder(target).first
                loc.fill(text, timeout=6000)
            except Exception:
                page.fill(target, text, timeout=6000)
            if kw.get("submit"):
                page.keyboard.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            return {"ok": True, "url": page.url}

        if action == "screenshot":
            out = Path(kw.get("path") or (profile_dir().parent / "browser-shots" /
                       f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"))
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=bool(kw.get("full_page")))
            return {"ok": True, "path": str(out), "url": page.url}

        if action == "tabs":
            return {"ok": True, "tabs": [
                {"index": i, "title": p.title(), "url": p.url}
                for i, p in enumerate(context.pages)
            ]}

        if action == "switch_tab":
            idx = int(kw.get("index") or 0)
            if 0 <= idx < len(context.pages):
                context.pages[idx].bring_to_front()
                return {"ok": True, "url": context.pages[idx].url}
            return {"ok": False, "error": f"no tab {idx}"}

        if action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=15000)
            return {"ok": True, "url": page.url, "title": page.title()}

        return {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        try:
            pw.stop()
        except Exception:
            pass
