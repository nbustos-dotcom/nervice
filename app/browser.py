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
# Full text of the LATEST Canvas scrape (both sources), overwritten each read. Lets us SEE exactly
# what was scraped from the real authenticated site instead of inferring it. Read-only artifact.
_CANVAS_DEBUG = _ROOT / "logs" / "canvas_last_read.log"
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


def _debug_dump(sources) -> None:
    """Write the FULL captured text of each Canvas source to _CANVAS_DEBUG (overwritten each read),
    so a real-site read can be inspected directly. `sources` = [(label, url, title, text), ...]."""
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        _CANVAS_DEBUG.parent.mkdir(exist_ok=True)
        with open(_CANVAS_DEBUG, "w", encoding="utf-8") as f:
            f.write(f"# Nervice read_canvas debug — {ts}\n")
            for label, url, title, text in sources:
                f.write(f"\n=== {label} ===\nurl:   {url}\ntitle: {title}\nchars: {len(text)}\n"
                        f"----- text -----\n{text}\n")
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


# Returns the first of `sels` actually present in the DOM (no waiting) — just for the audit log.
_WHICH_PRESENT_JS = """(sels) => { for (const s of sels) { try { if (document.querySelector(s)) return s; } catch(e){} } return null; }"""


async def _wait_for_render(page, selectors, settle_ms: int = 600, budget_ms: int = 6000) -> str | None:
    """Wait for async-hydrated content before reading. Canvas is a long-polling SPA, so it never
    reaches "networkidle" — the old wait_for_load_state("networkidle") just burned its full 15s
    timeout on every read (the dominant latency). The real "content is here" signal is a content
    region attaching, so wait for ANY of `selectors` in ONE bounded race (not a summed per-selector
    loop), then a short capped settle for late siblings. Tolerant — a timeout falls through (a
    genuinely empty agenda still reads, never mistaken for a load failure). Returns which selector
    matched (for the log), "content" if it matched but we couldn't tell which, or None."""
    hit = None
    try:
        await page.wait_for_selector(", ".join(selectors), timeout=budget_ms, state="attached")
        try:
            hit = await page.evaluate(_WHICH_PRESENT_JS, selectors)   # which one — no wait, log only
        except Exception:
            hit = "content"
    except Exception:
        pass
    try:
        await page.wait_for_timeout(settle_ms)                        # capped: late siblings only
    except Exception:
        pass
    return hit


# The Canvas agenda fetches its events by XHR AFTER the shell loads; reading at shell-load captured
# only a "Loading" placeholder (the real bug — the SAT2343 items live ONLY in the agenda). These are
# the agenda EVENT-ROW selectors (Canvas's own agenda renderer, plus fullcalendar list/grid views as
# fallbacks). _wait_for_agenda waits until real rows render — or an explicit empty state — and the
# "Loading" indicator is gone, before reading.
_AGENDA_EVENT_SEL = (".agenda-event__item, .ig-row, .agenda-day, .fc-list-item, .fc-list-event, "
                     ".fc-event, .calendar-event, .agenda-wrapper .ig-details")
_AGENDA_READY_JS = """(sel) => {
  const rows = document.querySelectorAll(sel).length;
  if (rows > 0) return true;                       // real event rows rendered -> done
  const root = document.querySelector('.agenda-wrapper,#agenda-view,#calendar-app,#content,#application') || document.body;
  const txt = (root.innerText || '');
  if (/\\bloading\\b/i.test(txt)) return false;    // no rows yet AND still loading -> keep waiting
  return /(nothing|no events|no assignments|no more items|you have no)/i.test(txt);  // empty state -> done
}"""


async def _wait_for_agenda(page, budget_ms: int = 10000) -> str:
    """Wait for the Canvas agenda to SETTLE before reading: real event rows rendered, OR an explicit
    'nothing scheduled' state — and the 'Loading' placeholder gone. The agenda is slower than the
    dashboard so the budget is larger. Tolerant: a timeout just proceeds to a best-effort read, and
    the debug dump records whatever was captured so a stuck agenda is visible, not silently empty.
    Returns a state string for the audit log: events(N) | settled-empty | timeout."""
    try:
        await page.wait_for_function(_AGENDA_READY_JS, arg=_AGENDA_EVENT_SEL, timeout=budget_ms)
        settled = True
    except Exception:
        settled = False
    try:
        await page.wait_for_timeout(400)                # tiny settle for the final rows
    except Exception:
        pass
    try:
        n = await page.evaluate("(sel) => document.querySelectorAll(sel).length", _AGENDA_EVENT_SEL)
    except Exception:
        n = -1
    if n and n > 0:
        return f"events({n})"
    return "settled-empty" if settled else "timeout"


async def _canvas_text(page) -> str:
    try:
        text = await page.evaluate(_CANVAS_TEXT_JS)
    except Exception:
        text = ""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# The agenda EVENT LIST only — the dated day/event rows — NOT the whole calendar body (reading
# #content/body pulled in the mini-month grid + toolbar AND the list TWICE, which confused the
# synthesis). Each row is collapsed to one line and deduped by text, so Canvas's repeated/overlapping
# render of the list becomes a clean, dated, deduped item list. Per-item "Open event menu" chrome is
# stripped. Returns '' if no agenda rows are found (caller falls back to the broad reader).
_AGENDA_TEXT_JS = """() => {
  const pick = ['.agenda-wrapper', '#agenda-view', '.fc-listView', '.fc-list'];
  let box = null;
  for (const s of pick) { const e = document.querySelector(s);
    if (e && (e.innerText||'').trim().length > 20) { box = e; break; } }
  const scope = box || document;
  const rows = scope.querySelectorAll('.agenda-day, .agenda-event__item, .fc-list-day, .fc-list-heading, .fc-list-item, .fc-list-event');
  if (!rows.length) return '';
  const seen = new Set(); const out = [];
  for (const n of rows) {
    let t = (n.innerText || '').replace(/\\s+/g,' ').trim();
    t = t.replace(/\\s*open event menu( for .*)?$/i, '').trim();   // drop the per-item kebab-menu label
    if (!t) continue;
    const k = t.toLowerCase();
    if (seen.has(k)) continue;                                     // dedupe repeated event blocks
    seen.add(k); out.push(t);
  }
  return out.join('\\n');
}"""

# Calendar UI chrome / month-grid lines to drop from the FALLBACK (broad-read) agenda text. Anchored
# whole-line (^(?:...)$) so it only kills unmistakable UI rows, never an assignment whose title merely
# starts with one of these words.
_CHROME_LINE = re.compile(
    r"^(?:open event menu\b.*|create new event\b.*|add event\b.*|change view\b.*|"
    r"select calendars?\b.*|find appointment\b.*|calendars?:?|"
    r"previous(?: month| week| day)?|next(?: month| week| day)?|today|go to today|"
    r"agenda|week|month|day|"
    r"(?:su|mo|tu|we|th|fr|sa)(?:\s+(?:su|mo|tu|we|th|fr|sa)){3,}|"   # weekday-abbrev header row
    r"\d{1,2}(?:\s+\d{1,2}){4,})$", re.I)                            # a row of month-grid day numbers


def _strip_chrome(text: str) -> str:
    """Drop calendar UI chrome + month-grid noise from broad-read agenda text. Line-by-line, NO
    dedupe (broad text is multi-line per event, so deduping lines would wrongly collapse repeated
    'Not Completed' rows). The structured reader above already excludes this noise; this is the net."""
    out = [s for ln in (text or "").splitlines() if (s := ln.strip()) and not _CHROME_LINE.match(s)]
    return "\n".join(out).strip()


async def _agenda_text(page) -> str:
    """Clean agenda EVENT-LIST text for the smart brain: the dated rows only, deduped, chrome-free.
    Structured read first (clean + deduped); falls back to the broad reader + chrome strip if the
    agenda-row selectors don't match, so items are never lost."""
    try:
        text = await page.evaluate(_AGENDA_TEXT_JS)
    except Exception:
        text = ""
    if (text or "").strip():
        return text.strip()[:_CANVAS_MAX // 2]
    broad = await _canvas_text(page)                 # safety net: the broad reader that already worked
    return _strip_chrome(broad)[:_CANVAS_MAX // 2]


async def _read_dash_region(page) -> dict:
    """Open the Canvas dashboard (read-only), detect a login wall, wait for the To Do / Coming Up
    content to hydrate, return its text. Shape: {error} | {needs_login,title,url} | {text,hit,title,url}."""
    try:
        await page.goto(CANVAS_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return {"error": f"Couldn't open Canvas ({type(e).__name__})."}
    title = (await page.title()) or ""
    if await _looks_like_login(page):
        return {"needs_login": True, "title": title, "url": page.url}
    hit = await _wait_for_render(page, _DASH_CONTENT)
    return {"text": await _canvas_text(page), "hit": hit, "title": title, "url": page.url}


async def _read_agenda_region(page, url) -> dict:
    """Open the Canvas calendar AGENDA view (read-only — the URL hash sets the view, no clicks),
    wait for its dated list to hydrate, return its text. Best-effort: a failure yields empty text
    (the dashboard still answers) rather than sinking the whole read."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for the AGENDA EVENTS to hydrate (not just the shell) — don't read while "Loading".
        state = await _wait_for_agenda(page)
        return {"text": await _agenda_text(page), "hit": f"agenda:{state}",
                "url": page.url, "title": (await page.title()) or ""}
    except Exception as e:
        print(f"[browser] agenda read failed: {repr(e)[:80]}", file=sys.stderr)
        return {"text": "", "hit": None, "url": url, "title": ""}


def _norm_line(s: str) -> str:
    """Normalize a line for dedupe: drop leading bullet/dash markers, collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", re.sub(r"^[\s•‣●\-\*]+", "", s)).strip().lower()


def _merge_canvas(dash: str, agenda: str) -> str:
    """Combine BOTH sources into one clean block for the smart brain, deduped at the line level so an
    item that appears in both the To Do pane AND the agenda is listed once. The dashboard section is
    kept in full; the agenda section then contributes only lines not already shown (so the dup lands
    in the dashboard section, not both). Each section and the whole are length-capped."""
    seen: set = set()

    def keep(block: str, cap: int) -> str:
        out = []
        for ln in block.splitlines():
            if not ln.strip():
                out.append("")
                continue
            k = _norm_line(ln)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            out.append(ln)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()[:cap]

    parts = []
    d = keep(dash, _CANVAS_MAX // 2)
    if d:
        parts.append("DASHBOARD — To Do / Coming Up:\n" + d)
    a = keep(agenda, _CANVAS_MAX // 2)   # seen now holds the dashboard lines -> agenda dups dropped
    if a:
        parts.append("CALENDAR AGENDA — upcoming items by date:\n" + a)
    return ("\n\n".join(parts))[:_CANVAS_MAX]


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
    page = await _current_page(ctx)
    agenda_url = CANVAS_URL.rstrip("/") + "/calendar#view_name=agenda"
    # Read BOTH sources, but SEQUENTIALLY on ONE page/session. Reading two pages CONCURRENTLY in the
    # same authenticated context raced Canvas's session (a Set-Cookie / CSRF rotation on one response
    # invalidated the other in-flight read) and BOTH came back empty on the REAL site, even though
    # the stand-in tolerated it. The original sequential read worked; its only problem was the
    # networkidle dead-wait, already removed — so sequential + the fast selector waits is both
    # correct AND ~2-3s. Read-only throughout. Per-source URL/title/char-count is logged, and the
    # full captured text of each source is dumped to _CANVAS_DEBUG so the real scrape is observable.

    # --- SOURCE 1: dashboard To Do / Coming Up ---
    dash_res = await _read_dash_region(page)
    if "error" in dash_res:                      # the dashboard navigation itself failed -> honest fail
        await _emit("read_canvas", CANVAS_URL, f"FAIL {dash_res['error']}")
        return {"ok": False, "error": dash_res["error"], "url": CANVAS_URL}
    title = dash_res.get("title", "")
    if dash_res.get("needs_login"):
        # Capture WHAT it landed on and tailor the honest message: no saved auth -> log in; auth
        # present but still bounced -> the session expired, refresh it. Never a fabricated list.
        landed = dash_res.get("url", CANVAS_URL)
        await _emit("read_canvas", landed, f"needs login (auth_cookies={injected}) landed={title!r}")
        msg = ("Canvas needs a login first — run scripts/canvas_login.py, log in once, then ask again."
               if injected == 0 else
               "Canvas logged me out (the saved session expired). Re-run scripts/canvas_login.py to "
               "refresh it, then ask again.")
        return {"ok": False, "needs_login": True, "landed_title": title, "landed_url": landed, "error": msg}
    dash, dash_hit = dash_res.get("text", ""), dash_res.get("hit")
    dash_url = dash_res.get("url", CANVAS_URL)
    await _emit("read_canvas:dashboard", dash_url,
                f"title={title!r} chars={len(dash)} waited={dash_hit or 'settle'}")

    # --- SOURCE 2: calendar agenda (SAME page, navigated AFTER the dashboard read finished) ---
    agenda_res = await _read_agenda_region(page, agenda_url)
    agenda, agenda_hit = agenda_res.get("text", ""), agenda_res.get("hit")
    agenda_landed, agenda_title = agenda_res.get("url", agenda_url), agenda_res.get("title", "")
    await _emit("read_canvas:agenda", agenda_landed,
                f"title={agenda_title!r} chars={len(agenda)} waited={agenda_hit or 'settle'}")

    # Full-text dump of BOTH raw sources (pre-merge) so the real scrape is visible, not inferred.
    _debug_dump([("SOURCE 1: DASHBOARD", dash_url, title, dash),
                 ("SOURCE 2: CALENDAR AGENDA", agenda_landed, agenda_title, agenda)])

    text = _merge_canvas(dash, agenda)           # both sources, deduped at the line level
    region = (f"{CANVAS_URL} (dashboard, waited={dash_hit or 'settle'}) "
              f"+ /calendar#agenda (waited={agenda_hit or 'settle'}) [sequential, both read]")
    # Honest "empty vs failed": we loaded + waited; if there's genuinely almost no content, say so
    # (the smart brain will tell Nate nothing's due) rather than pretend it failed.
    sparse = len(text.strip()) < 60
    await _emit("read_canvas", region,
                f"ok dash={len(dash)} agenda={len(agenda)} merged={len(text)} chars sparse={sparse}")
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
