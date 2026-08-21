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
import uuid
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .log import log_error

# Two lanes, because one desktop cannot hold two workers.
#
# LOUD is the browser above: headful, shared, on the owner's screen. It is
# the right tool when the owner should see what happens — and the wrong
# one the rest of the time, because a window that raises itself takes the
# keyboard out from under whatever they were typing.
#
# QUIET is a second Chrome with no window at all. Not minimised, not on
# another desktop — headless, so there is nothing that *can* be raised. It
# does the autonomous work: searching, fetching, reading.
LANE_LOUD = "loud"
LANE_QUIET = "quiet"

CDP_PORT = 18223
QUIET_CDP_PORT = 18225

# Headless Chrome announces itself in the User-Agent string as
# "HeadlessChrome", and Google answers that with a CAPTCHA: measured
# 2026-08-21, /sorry/index and zero results. The same profile with an
# ordinary Chrome UA returns full results. This is the whole difference
# between the quiet lane working and not.
QUIET_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Every tab this module opens starts at this marker so it can be found
# again without guessing. The marker only survives until the tab is
# navigated, so it identifies a tab stranded *before* navigation; tabs are
# otherwise closed in a finally block.
LISAN_TAB_MARKER = "lisan-agent-tab-"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def lane_port(lane: str) -> int:
    """The debug port for a lane. Unknown lanes are loud: a visible
    browser is the safe default, never a silent one."""
    return QUIET_CDP_PORT if str(lane or "").strip().lower() == LANE_QUIET else CDP_PORT


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


def quiet_profile_dir() -> Path:
    from ..paths import vault_root

    # sibling of the loud profile, same reasoning: inside the install,
    # never inside the repo
    return vault_root().parent / "browser-quiet-profile"


def quiet_chrome_args() -> list[str]:
    return [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={QUIET_CDP_PORT}",
        f"--user-data-dir={quiet_profile_dir()}",
        f"--user-agent={QUIET_USER_AGENT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
    ]


# Files that carry a Chrome session. "Local State" holds the key that
# decrypts the cookie jar; both profiles belong to the same OS user, so
# the keychain entry behind it is shared.
_SESSION_FILES = (
    ("Local State", ""),
    ("Cookies", "Default"),
    ("Preferences", "Default"),
    ("Secure Preferences", "Default"),
)


def seed_quiet_profile(force: bool = False) -> bool:
    """Give a new quiet profile the owner's existing session.

    Copied from the loud profile's files rather than synced over CDP,
    because a CDP sync would require the loud browser to be *running* —
    which would put a window on screen for the sake of avoiding windows.
    """
    import shutil

    source = profile_dir()
    target = quiet_profile_dir()
    if target.exists() and not force:
        return False
    if not source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return False
    (target / "Default").mkdir(parents=True, exist_ok=True)
    for name, subdir in _SESSION_FILES:
        src = source / subdir / name if subdir else source / name
        dst = target / subdir / name if subdir else target / name
        try:
            if src.is_file():
                shutil.copy2(src, dst)
        except Exception as exc:  # a partial seed is fine; sync_session repairs it
            log_error(None, f"quiet profile seed: {name}", exc)
    return True


def _clear_stale_singleton(profile: Path) -> bool:
    """Remove the lock a killed Chrome leaves behind.

    Chrome refuses to start on a profile holding a SingletonLock from a
    process that no longer exists, and aborts with "Failed to create a
    ProcessSingleton". For a headless browser nobody watches, that would
    mean one unclean shutdown disables the quiet lane permanently. Only
    ever called when the port is dead AND no process is using the
    profile, so a live browser's lock is never touched.
    """
    if _profile_in_use(profile):
        return False
    removed = False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        candidate = profile / name
        try:
            if candidate.is_symlink() or candidate.exists():
                candidate.unlink()
                removed = True
        except Exception as exc:
            log_error(None, f"stale singleton: {name}", exc)
    return removed


def _profile_in_use(profile: Path) -> bool:
    """Whether some Chrome process is currently running on this profile."""
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={profile}"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(found.stdout.strip())
    except Exception:
        # Unknown means "assume in use": refusing to clear a lock is
        # recoverable, clearing a live one corrupts a profile.
        return True


def _cdp_alive(timeout: float = 1.5, port: int = CDP_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser(wait_seconds: float = 20.0, lane: str = LANE_LOUD) -> bool:
    """The lane's browser is running with its debug port up, launching it
    if needed. Launched detached: it outlives every lisan process.

    Cold start is slower than it looks — Chrome's first run on a fresh
    profile has been seen to take longer than 15s, which read as "could
    not launch Chrome" when it was only slow.
    """
    quiet = str(lane or "").strip().lower() == LANE_QUIET
    port = lane_port(lane)
    if _cdp_alive(port=port):
        return True
    if quiet:
        seed_quiet_profile()
        quiet_profile_dir().mkdir(parents=True, exist_ok=True)
        _clear_stale_singleton(quiet_profile_dir())
    else:
        profile_dir().mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            quiet_chrome_args() if quiet else chrome_args(),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log_error(None, f"browser launch failed ({lane})", exc)
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _cdp_alive(port=port):
            return True
        time.sleep(0.4)
    return False


def _connect(pw: Any, lane: str) -> Any:
    """A Playwright context attached to one lane's running Chrome."""
    cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{lane_port(lane)}")
    return cdp.contexts[0] if cdp.contexts else cdp.new_context()


_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def _copy_cookies(source_ctx: Any, target_ctx: Any, *, source: str, target: str) -> dict[str, Any]:
    """Move one lane's cookie jar into the other's.

    Takes contexts rather than lane names so a caller already holding a
    Playwright session can reuse it: starting a second sync session inside
    the first raises "Sync API inside the asyncio loop".
    """
    cookies = source_ctx.cookies()
    payload = [{key: cookie[key] for key in _COOKIE_FIELDS if key in cookie} for cookie in cookies]
    before = len(target_ctx.cookies())
    if payload:
        target_ctx.add_cookies(payload)
    return {"ok": True, "source": source, "target": target, "copied": len(payload),
            "before": before, "after": len(target_ctx.cookies())}


def sync_session(source: str = LANE_LOUD, target: str = LANE_QUIET) -> dict[str, Any]:
    """Copy cookies from one lane to the other.

    This is what lets the quiet lane inherit a login the owner performed
    by hand, and what carries a fresh login back after a handoff. It is a
    snapshot, not a mirror: a token refreshed in one lane is stale in the
    other until the next sync, which is why handoff syncs on both sides of
    the owner's involvement.
    """
    if not ensure_browser(lane=source) or not ensure_browser(lane=target):
        return {"ok": False, "error": f"both lanes must be running to sync ({source} -> {target})"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install playwright)"}

    pw = sync_playwright().start()
    try:
        return _copy_cookies(_connect(pw, source), _connect(pw, target), source=source, target=target)
    except Exception as exc:
        log_error(None, f"session sync {source}->{target}", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            pw.stop()
        except Exception:
            pass


# Signals that a page wants a human: the quiet lane can read a login form
# but cannot satisfy one, and no amount of retrying changes that.
_WALL_URL_MARKERS = ("/sorry/", "accounts.google.com", "/login", "/signin", "/challenge", "captcha")
_WALL_TITLE_MARKERS = (
    "sign in", "log in", "login", "captcha", "access denied", "403 forbidden",
    "verify your", "just a moment",
)
_WALL_TEXT_MARKERS = (
    "unusual traffic", "verify you are human", "i'm not a robot", "are you a robot",
    "sign in to continue", "please log in", "enter your password", "two-factor",
    "verification code", "prove you're not a robot",
)


def looks_like_login_wall(url: str = "", title: str = "", text: str = "") -> bool:
    """Whether a page is asking for a human rather than answering.

    Deliberately conservative. A false positive here puts a window on the
    owner's screen for no reason, which is the exact interruption this
    design exists to remove — so a page that merely *mentions* signing in
    (most of the web) must not qualify. Titles count only when the phrase
    leads the title or the title is short enough to be the page's whole
    purpose; body text counts only for phrases a page uses when it is
    addressing the person rather than the reader.
    """
    if any(marker in str(url or "").lower() for marker in _WALL_URL_MARKERS):
        return True
    heading = str(title or "").strip().lower()
    if heading and any(
        heading.startswith(marker) or (len(heading) < 60 and marker in heading)
        for marker in _WALL_TITLE_MARKERS
    ):
        return True
    body = str(text or "").lower()[:4000]
    return any(marker in body for marker in _WALL_TEXT_MARKERS)


def browser_handoff(
    url: str,
    reason: str,
    *,
    wait_seconds: float = 300.0,
    poll_seconds: float = 2.0,
    notify: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Ask the owner to do the part only they can do.

    The one moment the loud lane is the right tool. The owner's session is
    carried over, a window opens on the page that needs them, and Telegram
    says why — a window that appears without an explanation is exactly the
    interruption this design exists to prevent. When they are done, the
    new cookies go back to the quiet lane and the tab closes.

    Completion is detected by the page leaving the wall, so the owner
    signals by simply finishing; there is nothing extra to click.
    """
    url = str(url or "").strip()
    if not url:
        return {"ok": False, "error": "handoff needs a url"}
    reason = str(reason or "").strip() or "I need your help with a page."
    if not ensure_browser(lane=LANE_LOUD):
        return {"ok": False, "error": "the visible browser could not be started"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install playwright)"}

    if not ensure_browser(lane=LANE_QUIET):
        return {"ok": False, "error": "the quiet browser could not be started"}
    pw = sync_playwright().start()
    page = None
    try:
        context = _connect(pw, LANE_LOUD)
        quiet_ctx = _connect(pw, LANE_QUIET)
        # Carry the working session in, so the owner is not asked to log
        # into something they are already logged into.
        carried = _copy_cookies(quiet_ctx, context, source=LANE_QUIET, target=LANE_LOUD)
        # Foreground on purpose: this is the one case where taking the
        # owner's attention IS the point.
        page = context.new_page()
        # A redirect-based flow can end at a URL the page never
        # successfully loads: OAuth sends the browser to a local port with
        # the code attached, nothing answers, and Chrome reports
        # chrome-error://chromewebdata/ — losing the only copy of the
        # result. The navigation *request* still happens, so record every
        # destination the tab attempts.
        visited: list[str] = []

        def _record_navigation(request: Any) -> None:
            try:
                if request.is_navigation_request():
                    visited.append(request.url)
            except Exception:
                pass

        page.on("request", _record_navigation)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _notify_owner_handoff(f"{reason}\n\nI opened it in your browser: {url}", notify=notify)
        deadline = time.time() + max(0.0, float(wait_seconds))
        resolved = False
        while time.time() < deadline:
            time.sleep(max(0.5, float(poll_seconds)))
            try:
                if page.is_closed():
                    # The owner closing the tab is a valid "done".
                    resolved = True
                    break
                current, title = page.url, page.title()
            except Exception:
                resolved = True
                break
            if not looks_like_login_wall(current, title):
                resolved = True
                break
        # Read the destination before closing: a redirect-based flow (OAuth
        # consent, magic links) leaves its result in the URL bar, and
        # closing the tab first throws it away.
        try:
            final_url = "" if page.is_closed() else page.url
        except Exception:
            final_url = ""
        returned = _copy_cookies(context, quiet_ctx, source=LANE_LOUD, target=LANE_QUIET)
        return {
            "ok": resolved, "url": url, "final_url": final_url, "visited": visited,
            "resolved": resolved,
            "waited_seconds": round(max(0.0, float(wait_seconds)) - max(0.0, deadline - time.time()), 1),
            "carried_to_loud": carried.get("copied", 0),
            "returned_to_quiet": returned.get("copied", 0),
            "error": None if resolved else "the owner did not complete the handoff in time",
        }
    except Exception as exc:
        log_error(None, "browser handoff", exc)
        return {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _notify_owner_handoff(text: str, *, notify: Callable[[str], bool] | None = None) -> bool:
    """Tell the owner why a window just appeared.

    Routed through the escalation notifier so it inherits its guards: the
    outbound kill switch, and the rule that only the resident install's
    own vault may reach the owner's phone.
    """
    if notify is not None:
        return bool(notify(text))
    try:
        from ..paths import vault_root
        from .escalation import _notify_owner

        return bool(_notify_owner(text, chat_id=None, vault=vault_root()))
    except Exception as exc:
        log_error(None, "handoff notify", exc)
        return False


def browser_action(action: str, lane: str = LANE_QUIET, **kw: Any) -> dict[str, Any]:
    """One browser operation: connect over CDP, act, detach. The browser
    itself keeps running (and keeps the owner's hands on it).

    Defaults to the quiet lane. Autonomous work belongs in a browser with
    no window; ``lane="loud"`` is for the times the owner should watch,
    and ``browser_handoff`` for the times they must act.
    """
    action = str(action or "").strip().lower()
    lane = str(lane or "").strip().lower() or LANE_QUIET
    if action == "open":
        ok = ensure_browser(lane=lane)
        where = "on screen" if lane == LANE_LOUD else "running quietly (no window)"
        return {"ok": ok, "lane": lane, "note": f"browser is {where}" if ok else "could not launch Chrome"}
    if not ensure_browser(lane=lane):
        return {"ok": False, "lane": lane, "error": f"the {lane} browser could not be started"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install playwright)"}

    pw = sync_playwright().start()
    try:
        cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{lane_port(lane)}")
        context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
        pages = [p for p in context.pages if not p.url.startswith("devtools")]
        page = pages[-1] if pages else context.new_page()
        try:
            # Playwright's CDP attach emulates prefers-color-scheme: light,
            # flipping the owner's dark theme every time the agent drives.
            # no-override hands appearance back to the system.
            page.emulate_media(color_scheme="no-override")
        except Exception:
            pass

        if action == "goto":
            url = str(kw.get("url") or "").strip()
            if not url:
                return {"ok": False, "error": "goto needs a url"}
            if "://" not in url:
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"ok": True, "url": page.url, "title": page.title()}

        if action == "read":
            # innerText forces a full layout pass and can freeze a heavy
            # page's main thread for seconds (Chrome's "Wait/Kill page"
            # dialog — seen live on the Google Cloud Console, even while
            # the owner was driving). A TreeWalker over textContent reads
            # the DOM without any layout work: never hangs the renderer.
            body = page.evaluate(
                """() => {
                    const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE']);
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    const parts = [];
                    let node, budget = 400000;
                    while ((node = walker.nextNode()) && budget > 0) {
                        if (skip.has(node.parentElement && node.parentElement.tagName)) continue;
                        const s = node.textContent.replace(/\\s+/g, ' ').trim();
                        if (s) { parts.push(s); budget -= s.length; }
                    }
                    return parts.join('\\n');
                }"""
            )
            limit = int(kw.get("max_chars") or 6000)
            return {"ok": True, "url": page.url, "title": page.title(),
                    "text": body[:limit], "truncated": len(body) > limit}

        if action == "elements":
            # Complex apps (Google Cloud Console class) defeat text-guessing.
            # Enumerate what is actually clickable/fillable, numbered, so the
            # next click/type can target by index — deterministic aiming.
            els = page.eval_on_selector_all(
                "a, button, [role=button], [role=link], [role=tab], [role=menuitem], "
                "input, select, textarea",
                """(nodes) => nodes
                    .filter(n => n.offsetParent !== null)
                    .slice(0, 120)
                    .map((n, i) => ({
                        index: i,
                        tag: n.tagName.toLowerCase(),
                        // textContent, never innerText: innerText forces a
                        // layout pass PER NODE and froze heavy pages
                        text: (n.textContent || n.value || n.placeholder || n.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
                        type: n.getAttribute('type') || undefined,
                    }))
                    .filter(e => e.text)""",
            )
            return {"ok": True, "url": page.url, "elements": els}

        if action == "click":
            target = str(kw.get("target") or "").strip()
            index = kw.get("index")
            if index is not None:
                els = page.query_selector_all(
                    "a, button, [role=button], [role=link], [role=tab], [role=menuitem], "
                    "input, select, textarea")
                visible = [e for e in els if e.is_visible()][:120]
                idx = int(index)
                if not (0 <= idx < len(visible)):
                    return {"ok": False, "error": f"no element {idx} (have {len(visible)})"}
                visible[idx].click(timeout=6000)
            elif target:
                try:
                    page.get_by_text(target, exact=False).first.click(timeout=6000)
                except Exception:
                    page.click(target, timeout=6000)
            else:
                return {"ok": False, "error": "click needs a target (visible text/CSS) or an index from 'elements'"}
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


# Search engines the owner's own browser can drive. The browser carries a
# real profile and real cookies, which is why this returns relevant results
# where a cookieless urllib request to the same engine got served decoy
# pages (2026-08-21).
SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "google": "https://www.google.com/search?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
}

# Result extraction is deliberately structural rather than class-name based:
# every engine renames its CSS classes eventually, but "an outbound link with
# real text, inside a result container" survives redesigns.
_EXTRACT_RESULTS = r"""(nodes) => {
    const junk = /^(accounts|policies|support|myaccount|maps|mail|translate|news\.google)\./;
    const seen = new Set();
    const out = [];
    for (const node of nodes) {
        const href = node.href;
        if (!href || !/^https?:/.test(href)) continue;
        let host = '';
        try { host = new URL(href).hostname.replace(/^www\./, ''); } catch (err) { continue; }
        if (window.__lisanEngineHosts.some(h => host === h || host.endsWith('.' + h))) continue;
        if (junk.test(host)) continue;
        const title = (node.textContent || '').replace(/\s+/g, ' ').trim();
        if (title.length < 8) continue;
        // Engines render a URL breadcrumb as its own anchor sharing the
        // result's href; taking it first gives every result a URL for a
        // title.
        if (/^https?:\/\//.test(title) || title.includes('\u203a')) continue;
        const key = href.split('#')[0];
        if (seen.has(key)) continue;
        seen.add(key);
        const box = node.closest('article, li, div[data-testid], div.g, div.b_algo');
        let snippet = box ? (box.textContent || '').replace(/\s+/g, ' ').trim() : '';
        if (snippet.startsWith(title)) snippet = snippet.slice(title.length).trim();
        out.push({url: key, title: title.slice(0, 300), snippet: snippet.slice(0, 1200)});
        if (out.length >= window.__lisanLimit) break;
    }
    return out;
}"""


def _open_background_target(context: Any, url: str) -> str:
    """Open a tab WITHOUT taking the owner's keyboard and mouse.

    Playwright's ``context.new_page()`` activates the Chrome window on
    macOS: measured 2026-08-21, frontmost went iTerm2 -> Google Chrome on
    every call, which stole keystrokes from the owner mid-sentence while
    the agent searched. Chrome's own ``Target.createTarget`` takes a
    ``background`` flag that ``new_page()`` does not expose; driving it
    over raw CDP leaves the frontmost application untouched.

    Returns the unique blank URL the tab was opened at. Playwright does
    not enumerate targets it did not attach to, so the caller reconnects
    to pick the tab up, then navigates it to the real destination.
    """
    anchor = context.pages[0] if context.pages else context.new_page()
    session = context.new_cdp_session(anchor)
    # A unique blank marker, not the destination: two runs of the same
    # search would otherwise produce two tabs with identical URLs and no
    # way to tell which one is ours.
    marker = f"about:blank#{LISAN_TAB_MARKER}{uuid.uuid4().hex}"
    session.send("Target.createTarget", {"url": marker, "background": True})
    return marker


def browser_search(
    query: str,
    *,
    limit: int = 8,
    engine: str = "duckduckgo",
    settle_seconds: float = 2.5,
    lane: str = LANE_QUIET,
) -> dict[str, Any]:
    """Run one search in the owner's browser and return extracted results.

    Uses a dedicated tab that is closed afterwards, so the owner's own tabs
    are never navigated out from under them.

    Every failure is returned as ``{"ok": False, "error": ...}``. A search
    that cannot run must never look like a search that found nothing —
    that confusion is what made the previous backend dangerous.
    """
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "search needs a query"}
    template = SEARCH_ENGINES.get(str(engine or "").strip().lower())
    if not template:
        return {"ok": False, "error": f"unknown search engine: {engine!r}"}
    lane = str(lane or "").strip().lower() or LANE_QUIET
    if not ensure_browser(lane=lane):
        return {"ok": False, "error": f"the {lane} browser could not be started"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install playwright)"}

    import urllib.parse

    url = template.format(query=urllib.parse.quote_plus(query))
    engine_hosts = sorted({
        urllib.parse.urlparse(value.format(query="x")).hostname.replace("www.", "")
        for value in SEARCH_ENGINES.values()
    } | {"duck.ai", "spreadprivacy.com", "microsoft.com", "bing.net"})

    pw = sync_playwright().start()
    page = None
    context = None
    try:
        cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{lane_port(lane)}")
        context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
        marker = _open_background_target(context, url)
        # Reconnect so Playwright enumerates the tab CDP just created.
        page = None
        deadline = time.time() + 15.0
        while time.time() < deadline and page is None:
            time.sleep(0.4)
            cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{lane_port(lane)}")
            context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
            page = next((item for item in context.pages if item.url == marker), None)
        if page is None:
            return {"ok": False, "engine": engine, "error": "background tab did not attach"}
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(max(0.0, float(settle_seconds)))
        page.evaluate(
            "([hosts, limit]) => { window.__lisanEngineHosts = hosts; window.__lisanLimit = limit; }",
            [engine_hosts, max(1, int(limit))],
        )
        results = page.eval_on_selector_all("a[href]", _EXTRACT_RESULTS)
        if not results:
            # An engine that shows a consent wall or a CAPTCHA renders no
            # outbound links. The owner can take the mouse and clear it.
            walled = looks_like_login_wall(page.url, page.title())
            return {
                "ok": False, "engine": engine, "lane": lane, "url": page.url,
                # Tell the caller an escalation is available rather than
                # leaving it to guess: a wall is answerable by the owner,
                # changed markup is not.
                "needs_handoff": walled,
                "error": ("blocked by a consent wall or CAPTCHA" if walled
                          else "no results extracted (changed markup?)"),
            }
        return {"ok": True, "engine": engine, "lane": lane, "url": page.url, "results": results}
    except Exception as exc:
        log_error(None, "browser search failed", exc)
        return {"ok": False, "engine": engine, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:
            pass
        try:
            # A run killed between opening and navigating leaves a blank
            # marker tab in the owner's window. Sweep those.
            for item in list(context.pages if context is not None else []):
                if LISAN_TAB_MARKER in item.url:
                    item.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
