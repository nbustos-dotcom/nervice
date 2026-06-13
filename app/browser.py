"""Phase 1 of screen control: READ-ONLY browser perception via Playwright.

Nervice opens a page in its OWN dedicated, visible browser profile and READS the page's real
structure (DOM text / accessibility) — it does NOT click, type, submit, fill, or change anything.
Acting is a deliberately-LATER phase: no acting code exists here, so none can misfire. The safety
bones (read-only by construction, kill switch, audit) are built now because later phases inherit
them.

- SEMANTIC reading only (page.inner_text of the meaningful content) — never pixel/coordinate guessing.
- A dedicated PERSISTENT profile at data/nervice_browser_profile/ (gitignored) that owns nothing —
  separate from Nate's real Brave. Nate logs into Canvas ONCE inside it (scripts/canvas_login.py);
  the session then persists in the profile dir.
- VISIBLE (headless=False) so Nate watches everything.
- Every action is audited to logs/browser_actions.log AND (on a WS turn) emitted as a {type:"hands"}
  trace frame via the hands_trace contextvar, so the HUD trace console can show a live hands feed.
"""
import app.net  # noqa  (truststore: Norton TLS interception — needed for HTTPS navigation)

import asyncio
import contextvars
import datetime
import os
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROFILE_DIR = _ROOT / "data" / "nervice_browser_profile"
# Saved login (Playwright storage_state) written by scripts/canvas_login.py. It captures the SESSION
# cookies (MTU's CAS TGC, Canvas canvas_session) that a persistent profile dir does NOT restore on a
# fresh launch — re-injected before each Canvas read so the session actually carries. Gitignored.
_AUTH_STATE = _ROOT / "data" / "nervice_browser_auth.json"
_AUDIT_LOG = _ROOT / "logs" / "browser_actions.log"
_MAX_TEXT = 8000   # cap the read text so a huge page can't blow the LLM context

# Nate fills this in with his school's Canvas dashboard URL, e.g. "https://<school>.instructure.com".
# Left empty on purpose: read_canvas() returns an honest "not configured" message until it's set.
CANVAS_URL = "https://mtu.instructure.com"

# Read-only by construction: there is NO click/type/fill/submit primitive in this module. A caller
# that asks for a page ACTION gets this honest refusal — acting is a later phase.
NOT_SUPPORTED = ("I can read pages right now, but I can't click, type, or submit anything yet — "
                 "acting on a page is a later phase. For now I'll just read it.")

# Set per WS turn (streaming.py) to an async callback that ships a {type:"hands"} frame to the HUD;
# None on REST turns (audit log only). Mirrors app.agent.current_rung's per-turn contextvar pattern.
hands_trace: "contextvars.ContextVar" = contextvars.ContextVar("hands_trace", default=None)

_pw = None        # the started async_playwright manager
_context = None   # the persistent browser context (the singleton)
_lock = asyncio.Lock()


def _audit(action: str, target: str, outcome: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        _AUDIT_LOG.parent.mkdir(exist_ok=True)
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {action} {target} -> {outcome}\n")
    except Exception:
        pass


async def _emit(action: str, target: str, outcome: str) -> None:
    """Audit always; emit a live hands trace frame too when a WS turn set the callback."""
    _audit(action, target, outcome)
    cb = hands_trace.get()
    if cb is not None:
        try:
            await cb(action, target, outcome)
        except Exception:
            pass


async def _ensure_context():
    """Lazy-launch the dedicated, VISIBLE persistent context (singleton, guarded by a lock so two
    concurrent reads can't double-launch)."""
    global _pw, _context
    if _context is not None:
        return _context
    async with _lock:
        if _context is not None:
            return _context
        from playwright.async_api import async_playwright   # lazy: importing this module stays cheap
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw = await async_playwright().start()
        # VISIBLE by default so Nate watches everything; NERVICE_BROWSER_HEADLESS=1 forces headless
        # (used by the automated test suite so it needn't pop windows / hold a foreground desktop).
        headless = os.environ.get("NERVICE_BROWSER_HEADLESS") == "1"
        _context = await _pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR), headless=headless,
            args=["--no-first-run", "--no-default-browser-check"])
        print(f"[browser] launched visible persistent context at {_PROFILE_DIR}", file=sys.stderr)
        return _context


def is_open() -> bool:
    return _context is not None


async def _current_page(ctx):
    return ctx.pages[-1] if ctx.pages else await ctx.new_page()


async def _inject_auth(ctx) -> int:
    """Re-inject the saved login captured by scripts/canvas_login.py (Playwright storage_state),
    which includes the SESSION cookies the persistent profile can't restore. Idempotent — called
    before each Canvas read, so a fresh re-login is picked up without a restart. Returns the cookie
    count injected (0 = no saved auth yet)."""
    if not _AUTH_STATE.exists():
        return 0
    try:
        import json
        cookies = json.loads(_AUTH_STATE.read_text(encoding="utf-8")).get("cookies", [])
        if cookies:
            await ctx.add_cookies(cookies)
        return len(cookies)
    except Exception as e:
        print(f"[browser] auth inject failed: {repr(e)[:80]}", file=sys.stderr)
        return 0


def _https_only(url: str) -> str | None:
    """Accept https only; reject http and every non-web scheme (file:, data:, javascript:, ...)."""
    u = (url or "").strip()
    return u if re.match(r"^https://[^\s]+$", u, re.I) else None


async def open_page(url: str) -> dict:
    """Navigate to an https URL, wait for load, return {ok, title, url}. https only."""
    u = _https_only(url)
    if u is None:
        await _emit("open", url or "(empty)", "REJECTED non-https")
        return {"ok": False, "error": "Only https URLs are allowed.", "url": url}
    try:
        ctx = await _ensure_context()
        page = await _current_page(ctx)
        await page.goto(u, wait_until="domcontentloaded", timeout=30000)
        title, final = (await page.title()) or "", page.url
        await _emit("open", u, f"ok title={title!r} final={final}")
        return {"ok": True, "title": title, "url": final}
    except Exception as e:
        await _emit("open", u, f"FAIL {type(e).__name__}: {repr(e)[:80]}")
        return {"ok": False, "error": f"Couldn't open the page ({type(e).__name__}).", "url": u}


_EXTRACT_JS = """() => {
  const main = document.querySelector('main,[role="main"],#content,.ic-Dashboard,.ic-app-main-content,#main');
  const el = main || document.body;
  // innerText is the RENDERED text: it excludes <script>/<style> and respects visibility, and it
  // keeps headings, link text, list and table cells as readable lines. No DOM mutation, pure read.
  return (el && el.innerText) ? el.innerText : (document.body ? document.body.innerText : '');
}"""


async def _readable(page) -> str:
    try:
        text = await page.evaluate(_EXTRACT_JS)
    except Exception:
        text = ""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_MAX_TEXT]


async def read_page() -> dict:
    """Extract the current page's meaningful text as clean readable text (NOT raw HTML, NOT a
    screenshot). Length-capped."""
    if _context is None:
        return {"ok": False, "error": "No browser/page open."}
    page = await _current_page(_context)
    text = await _readable(page)
    await _emit("read", page.url, f"ok {len(text)} chars")
    return {"ok": True, "title": (await page.title()) or "", "url": page.url, "text": text}


_LOGIN_URL = re.compile(r"(login|sign[_-]?in|/auth|sso|oauth|cas/)", re.I)


async def _looks_like_login(page) -> bool:
    if _LOGIN_URL.search(page.url or ""):
        return True
    try:
        if await page.locator("input[type=password]").count() > 0:
            return True
    except Exception:
        pass
    return False


# Canvas hydrates its content with JavaScript AFTER load, so reading at DOMContentLoaded gets the
# skeleton ("Dashboard", no assignments). These are the regions/selectors where upcoming work
# actually renders; we wait for one to attach, then read.
_CANVAS_MAX = 12000   # bigger cap than a normal page — two Canvas regions, dated lists
_DASH_CONTENT = [".PlannerApp", ".planner-item", "[data-testid='todo-list']",
                 ".Sidebar__TodoListContainer", ".coming_up", ".ic-DashboardCard", "#content"]
_AGENDA_CONTENT = [".fc-listView", ".fc-agendaView", ".agenda-wrapper", "#agenda-view",
                   ".fc-list-item", ".fc-event", ".fc-view-container", "#content"]
# Read the main content column + the right sidebar (To Do / Coming Up) + the planner/agenda — NOT
# the global nav chrome. Falls back to the whole app/body if those regions are sparse.
_CANVAS_TEXT_JS = """() => {
  const want = ['#content','#right-side','.PlannerApp','#dashboard-planner','#agenda-view',
                '.agenda-wrapper','.fc-listView','.fc-view-container','.Sidebar__Container'];
  const seen = new Set(); const out = [];
  for (const s of want) { const e = document.querySelector(s);
    if (e && !seen.has(e)) { seen.add(e); const t = (e.innerText||'').trim(); if (t) out.push(t); } }
  let joined = out.join('\\n\\n');
  if (joined.replace(/\\s+/g,'').length < 40) {
    const a = document.querySelector('#application') || document.body; joined = a ? (a.innerText||'') : ''; }
  return joined;
}"""


async def _wait_for_render(page, selectors, settle_ms: int = 1500) -> str | None:
    """Wait for async-hydrated content before reading: let the network settle, then wait for any of
    `selectors` to attach, then a short settle. Every step is tolerant — a timeout just falls through
    (so a genuinely empty agenda still reads, it isn't mistaken for a load failure). Returns the
    selector that matched, or None (fell through to the settle)."""
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    hit = None
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=3000, state="attached")
            hit = sel
            break
        except Exception:
            continue
    try:
        await page.wait_for_timeout(settle_ms)
    except Exception:
        pass
    return hit


async def _canvas_text(page) -> str:
    try:
        text = await page.evaluate(_CANVAS_TEXT_JS)
    except Exception:
        text = ""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def read_canvas() -> dict:
    """Canvas-aware reader: open CANVAS_URL and return its clean readable text (assignments, due
    dates, announcements as they appear). If a login wall is detected, return an HONEST needs-login
    message — never a fabricated assignment list. The smart brain interprets the returned text."""
    if not CANVAS_URL:
        await _emit("read_canvas", "(unset)", "CANVAS_URL not configured")
        return {"ok": False, "error": "Canvas isn't set up yet — CANVAS_URL is blank in app/browser.py. "
                                      "See the Canvas setup steps."}
    ctx = await _ensure_context()
    injected = await _inject_auth(ctx)          # re-add saved session cookies BEFORE navigating
    opened = await open_page(CANVAS_URL)
    if not opened["ok"]:
        return opened
    page = await _current_page(_context)
    title = (await page.title()) or ""
    if await _looks_like_login(page):
        # Capture WHAT it landed on (point-4 diagnostics) and tailor the honest message: no saved
        # auth -> log in; auth present but still bounced -> the session expired, refresh it.
        await _emit("read_canvas", page.url, f"needs login (auth_cookies={injected}) landed={title!r}")
        msg = ("Canvas needs a login first — run scripts/canvas_login.py, log in once, then ask again."
               if injected == 0 else
               "Canvas logged me out (the saved session expired). Re-run scripts/canvas_login.py to "
               "refresh it, then ask again.")
        return {"ok": False, "needs_login": True, "landed_title": title, "landed_url": page.url, "error": msg}
    # Logged in. Canvas hydrates async — WAIT for the To Do / Coming Up content to render, then read
    # that sidebar region (the sparse card-dashboard main is why it only saw "Dashboard" before).
    dash_hit = await _wait_for_render(page, _DASH_CONTENT)
    dash = await _canvas_text(page)
    # The dated "what's due" list lives in the calendar AGENDA view — navigate there explicitly
    # (URL hash sets the view; still read-only, no clicks) and read it too.
    agenda, agenda_hit, agenda_url = "", None, CANVAS_URL.rstrip("/") + "/calendar#view_name=agenda"
    try:
        await page.goto(agenda_url, wait_until="domcontentloaded", timeout=30000)
        agenda_hit = await _wait_for_render(page, _AGENDA_CONTENT)
        agenda = await _canvas_text(page)
    except Exception as e:
        print(f"[browser] agenda read failed: {repr(e)[:80]}", file=sys.stderr)
    parts = []
    if dash.strip():
        parts.append("DASHBOARD — To Do / Coming Up:\n" + dash[:_CANVAS_MAX // 2])
    if agenda.strip():
        parts.append("CALENDAR AGENDA — upcoming items by date:\n" + agenda[:_CANVAS_MAX // 2])
    text = ("\n\n".join(parts))[:_CANVAS_MAX]
    region = f"{CANVAS_URL} (dashboard, waited={dash_hit or 'settle'}) + /calendar#agenda (waited={agenda_hit or 'settle'})"
    # Honest "empty vs failed": we loaded + waited; if there's genuinely almost no content, say so
    # (the smart brain will tell Nate nothing's due) rather than pretend it failed.
    sparse = len(text.strip()) < 60
    await _emit("read_canvas", region, f"ok dash={len(dash)} agenda={len(agenda)} chars sparse={sparse}")
    return {"ok": True, "url": region, "title": title, "text": text,
            "regions": {"dashboard_chars": len(dash), "agenda_chars": len(agenda)}, "sparse": sparse}


async def close_browser() -> dict:
    """KILL SWITCH: tear the context down instantly. Safe to call when nothing is open."""
    global _pw, _context
    ctx, pw = _context, _pw
    _context, _pw = None, None
    closed = ctx is not None
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    await _emit("kill", "browser", "closed" if closed else "nothing open")
    return {"ok": True, "closed": closed}


# --- in-turn recognition helpers (used by chat.respond / streaming.stream_reply) ----------------
_KILL_EXPLICIT = re.compile(r"\b(kill|close|stop|shut|quit|kill off|shut down)\s+(the\s+|that\s+)?browser\b", re.I)
_KILL_BARE = re.compile(r"^\s*(stop|stop it|kill it|cancel|abort)\s*[.!]?\s*$", re.I)
# Page-action verbs (read-only phase refuses these). Word-boundary so "click" etc. as a request verb.
_PAGE_ACTION = re.compile(r"\b(click|tap|press|type|fill|enter|submit|select|check|uncheck|toggle|"
                          r"scroll|drag|upload|download|log\s*in|sign\s*in|post|send|buy|add to cart)\b", re.I)


def is_kill_request(msg: str) -> bool:
    """A request to tear down the browser. Explicit '...browser' always counts; a bare 'stop'/'kill
    it' counts ONLY while a browser is actually open (so normal 'stop' usage isn't hijacked)."""
    m = msg or ""
    if _KILL_EXPLICIT.search(m):
        return True
    return is_open() and bool(_KILL_BARE.match(m))


def is_page_action(msg: str) -> bool:
    """True if the message asks to ACT on a page (not supported in this read-only phase)."""
    return bool(_PAGE_ACTION.search(msg or ""))
