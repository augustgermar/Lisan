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

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed (pip install playwright)"}

    pw = sync_playwright().start()
    try:
        cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
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


def browser_search(
    query: str,
    *,
    limit: int = 8,
    engine: str = "duckduckgo",
    settle_seconds: float = 2.5,
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
    if not ensure_browser():
        return {"ok": False, "error": "browser could not be started"}
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
    try:
        cdp = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
        page = context.new_page()
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
            return {
                "ok": False, "engine": engine, "url": page.url,
                "error": "no results extracted (consent wall, CAPTCHA, or changed markup)",
            }
        return {"ok": True, "engine": engine, "url": page.url, "results": results}
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
            pw.stop()
        except Exception:
            pass
