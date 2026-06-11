"""LocalComputer — Nervice's hands on the local Windows machine (the MSI).

This is the first capability that acts OUTSIDE the workspace jail, so the safety model is the
whole point and it is enforced in CODE, never by model judgment:

  CLOSED ACTION LIST. The only things Nervice can physically do are five named actions —
  open_app, open_url, screenshot, list_windows, focus_window. There is NO freeform-shell action.
  A request that doesn't map to one of these cannot be executed, period (the OpenClaw firewall).

  TWO-TIER POLICY. interpret() deterministically classifies a request as SAFE or RISKY:
    SAFE  (auto-exec): open a WHITELISTED app, open a URL/known site, screenshot, list windows,
                       focus/switch a window.
    RISKY (confirm) : open a NON-whitelisted app (doable after a yes), or any destructive / system
                      / send / credential / money request (NOT in the action list — never doable).
  Anything not clearly SAFE defaults to RISKY. RISKY never executes without an explicit yes in the
  next turn (the pending-confirmation gate, resolved by resolve_pending()).

  AUDIT. Every decision and action appends to logs/computer_actions.log.

Scope note: actions happen on THIS machine (the MSI). When Nate is on the phone, "open youtube"
opens it on the MSI's screen, not the phone's — "open on the phone's own screen" is a separate
future piece (would need a client-side deep-link).
"""
import os
import re
import sys
import time
import shutil
import pathlib
import datetime
import subprocess

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_AUDIT_LOG = _ROOT / "logs" / "computer_actions.log"
_SHOTS = _ROOT / "screenshots"

# ---------------------------------------------------------------------------
# Nate edits these two lists. WHITELIST = apps that open with no confirmation.
# Friendly name (lowercase) -> launch target (resolved on PATH or via Windows `start`/App Paths).
# ---------------------------------------------------------------------------
WHITELIST: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe",
    "paint": "mspaint.exe",
    "chrome": "chrome", "google chrome": "chrome",
    "brave": "brave", "brave browser": "brave",
    "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify",
    "vscode": "code", "vs code": "code", "code": "code",
}

# Friendly site names -> URL, so "open youtube" reliably opens the right place. Anything with a
# real domain (reddit.com, https://...) is also treated as a safe URL even if not listed here.
SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.com",
    "github": "https://github.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "netflix": "https://www.netflix.com",
    "twitter": "https://twitter.com", "x": "https://x.com",
    "youtube music": "https://music.youtube.com",
    "wikipedia": "https://www.wikipedia.org",
}

# ---------------------------------------------------------------------------
# Browsers. Nate changes the default in ONE line: BROWSER = "brave" | "chrome" | "edge" |
# "firefox" | "default" (the OS default). open_url() launches this browser's real exe with the URL
# as an argument; if the chosen browser isn't installed it falls back Brave -> Chrome -> OS default
# (logged), so a URL still opens. A request that NAMES a browser ("...in chrome") never falls back
# silently — if that browser is missing it says so plainly rather than using a different one.
# ---------------------------------------------------------------------------
BROWSER = "brave"

_BROWSER_ALIASES = {
    "brave": "brave", "brave browser": "brave",
    "chrome": "chrome", "google chrome": "chrome", "chrome browser": "chrome",
    "edge": "edge", "microsoft edge": "edge", "msedge": "edge", "edge browser": "edge",
    "firefox": "firefox", "mozilla firefox": "firefox", "mozilla": "firefox", "firefox browser": "firefox",
}
_BROWSER_EXE = {"brave": "brave.exe", "chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}


def _browser_candidates(canon: str) -> list[str]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    table = {
        "brave": [rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
                  rf"{pf86}\BraveSoftware\Brave-Browser\Application\brave.exe",
                  rf"{local}\BraveSoftware\Brave-Browser\Application\brave.exe"],
        "chrome": [rf"{pf}\Google\Chrome\Application\chrome.exe",
                   rf"{pf86}\Google\Chrome\Application\chrome.exe",
                   rf"{local}\Google\Chrome\Application\chrome.exe"],
        "edge": [rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
                 rf"{pf}\Microsoft\Edge\Application\msedge.exe"],
        "firefox": [rf"{pf}\Mozilla Firefox\firefox.exe",
                    rf"{pf86}\Mozilla Firefox\firefox.exe"],
    }
    return table.get(canon, [])


def _app_paths_lookup(exe_name: str) -> str | None:
    """The Windows 'App Paths' registry key is where browsers register their real exe location —
    a robust fallback when the app isn't on PATH or in the usual Program Files spot."""
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
                with winreg.OpenKey(root, key) as k:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val and os.path.isfile(val):
                        return val
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return None


def _browser_exe(name: str) -> tuple[str | None, str | None]:
    """Resolve a browser friendly-name to (existing exe path, canonical name), or (None, canon)
    if that browser isn't installed. Pure filesystem/registry checks — no launching."""
    canon = _BROWSER_ALIASES.get(name.lower().strip())
    if not canon:
        return None, None
    for p in _browser_candidates(canon):
        if p and os.path.isfile(p):
            return p, canon
    exe = _BROWSER_EXE[canon]
    via = _app_paths_lookup(exe) or shutil.which(exe)
    return (via, canon) if via else (None, canon)


def detect_browsers() -> dict[str, str | None]:
    """Which of the four known browsers are installed -> their exe path (or None). For the report."""
    return {canon: _browser_exe(canon)[0] for canon in ("brave", "chrome", "edge", "firefox")}


# NOTE: "run" is deliberately NOT an open-verb — "run <x>" reads as shell-exec intent, which must
# never map to a launch. It falls through to the fail-safe RISKY branch instead.
_OPEN_VERBS = r"open(?:\s+up)?|launch|start|fire\s+up|bring\s+up|pull\s+up|go\s+to|visit|load|put\s+on"
_SWITCH_VERBS = r"switch\s+to|focus(?:\s+on)?|activate|bring\s+.*?\bto\s+the\s+front"
# words that make a request RISKY regardless of anything else (destructive / send / money / system /
# shell). A shell or shell-command token here means "open cmd", "run rm -rf", etc. never auto-exec.
_RISKY_WORDS = re.compile(
    r"\b(delete|remove|erase|wipe|format|uninstall|trash|empty|move|rename|overwrite|"
    r"close|quit|kill|terminate|end\s+task|shut\s*down|shutdown|restart|reboot|log\s*off|sign\s*out|"
    r"send|e-?mail|post|tweet|publish|share|dm|message|"
    r"buy|purchase|order|pay|checkout|transfer|venmo|"
    r"password|credential|api\s*key|login|registry|"
    r"settings|preferences|disable|enable|install|update\s+windows|"
    r"rm|rmdir|del|sudo|powershell|terminal|command\s*prompt|cmd|bash|\bsh\b|exec|script)\b", re.I)

_SCREENSHOT = re.compile(r"\b(screenshot|screen\s*shot|screen\s*grab|grab\s+(?:the\s+)?screen|"
                         r"capture\s+(?:the\s+|my\s+)?screen|take\s+a\s+(?:picture|pic)\s+of\s+(?:the\s+)?screen)\b", re.I)
_LIST_WINDOWS = re.compile(
    r"\b(list|show)\b.{0,25}\b(window|app|program|running)s?\b"   # "list windows", "show whats running"
    r"|\bwhat'?s?\b.{0,25}\b(running|open)\b"                      # "what apps are running", "whats open"
    r"|\bwhat'?s?\b.{0,25}\b(window|app|program)s?\b.{0,15}\b(running|open|up)\b", re.I)

_AFFIRM = re.compile(r"^\s*(yes|yeah|yep|yup|ylep|sure|ok|okay|do\s+it|go\s+ahead|please\s+do|"
                     r"confirm(?:ed)?|affirmative|go\s+for\s+it|proceed|absolutely)\b", re.I)
_DENY = re.compile(r"^\s*(no|nope|nah|cancel|stop|don'?t|do\s+not|never\s*mind|nevermind|"
                   r"forget\s+it|abort|leave\s+it)\b", re.I)

_SAFE_NAME = re.compile(r"^[\w .+\-]{1,60}$")   # what we'll allow as a launch target (no shell metachars)
_PENDING_TTL = 300                              # seconds a confirmation request stays live


# ---------------------------------------------------------------------------
# Decision + audit
# ---------------------------------------------------------------------------

class Decision:
    """The deterministic verdict for one control request."""
    def __init__(self, action, target, risk, supported, reason, raw, browser=None):
        self.action = action          # 'open_app'|'open_url'|'screenshot'|'list_windows'|'focus_window'|None
        self.target = target          # app name / url / window substring / ''
        self.risk = risk              # 'safe' | 'risky'
        self.supported = supported    # can we actually do it (False for delete/close/send/etc.)
        self.reason = reason          # why risky, for the spoken confirmation
        self.raw = raw                # original message
        self.browser = browser        # for open_url: a specific browser the user named, else None


def _audit(kind: str, detail: str) -> None:
    try:
        _AUDIT_LOG.parent.mkdir(exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{kind}\t{detail}\n")
    except Exception as e:
        print(f"[computer audit failed] {repr(e)[:80]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Interpretation (the safety boundary — pure, deterministic, no model)
# ---------------------------------------------------------------------------

def _clean_target(t: str) -> str:
    t = t.strip().strip(".!?,")
    t = re.sub(r"\b(for\s+me|please|now|real\s+quick|quick)\b", "", t, flags=re.I).strip()
    t = re.sub(r"^(the|my|an?)\s+", "", t, flags=re.I).strip()
    t = re.sub(r"^(app|application|website|site|web\s*page|program)\s+", "", t, flags=re.I).strip()
    return t


def _looks_like_url(t: str) -> str | None:
    """Return a normalized URL if t is a site name or a domain, else None."""
    key = t.lower().strip()
    if key in SITES:
        return SITES[key]
    if re.match(r"^https?://", key):
        return t.strip()
    # bare domain like reddit.com or news.ycombinator.com
    if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", key) and " " not in key:
        return "https://" + key
    return None


def _to_url(site: str) -> str:
    """Best-effort URL for a site phrase the user wants opened in a named browser. Known site or
    domain resolves exactly; anything else becomes a https:// guess (it just navigates — opening a
    URL is SAFE regardless of where it lands)."""
    return _looks_like_url(site) or ("https://" + re.sub(r"\s+", "", site.strip()))


def _split_browser(target: str) -> tuple[str, str | None]:
    """Pull a trailing '... in/on/using <browser>' off the target. Only treats it as a browser
    directive when the named thing actually resolves to a known browser, so 'open youtube in
    spanish' isn't misread. Returns (site_part, canonical_browser_or_None)."""
    m = re.search(r"^(.*?)\s+(?:in|on|using|with|through)\s+(?:a\s+|the\s+|my\s+)?(.+)$", target, re.I)
    if not m:
        return target, None
    cand = re.sub(r"\s+browser$", "", m.group(2).strip().lower())
    canon = _BROWSER_ALIASES.get(cand)
    if canon:
        return m.group(1).strip(), canon
    return target, None


# What Nervice can actually do — quoted in every honest refusal so the boundary is always clear.
_CAPS = ("I can only open apps and websites, play something on YouTube, take screenshots, "
         "and switch windows")

# ---- play on YouTube: resolve a REAL video via yt-dlp, open the watch URL (auto-plays) ----
# This composes the existing SAFE open_url action with a read-only search resolution — no new
# boundary surface. Bare "play music/something/it" picks from a broad music list.
_PLAY_RE = re.compile(r"^\s*(?:please\s+|hey\s+|actually\s+|can\s+you\s+|could\s+you\s+|now\s+|"
                      r"just\s+|go\s+ahead\s+and\s+)*play\s+(.+)$", re.I)
_PLAY_BARE = {"it", "that", "this", "something", "anything", "music", "some music", "a song",
              "a video", "me a song", "me something", "me some music", "songs", "some songs",
              "something good", "whatever", "some tunes", "tunes"}
_MUSIC_QUERIES = ["lofi hip hop radio", "classic rock greatest hits", "top hits 2026 playlist",
                  "relaxing jazz piano", "best of 80s music mix", "chill electronic mix"]


def _resolve_youtube(query: str):
    """ytsearch1 -> (video_id, title) or (None, None). Read-only network search."""
    import app.net  # noqa  (truststore — Norton TLS; no-op if already injected)
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch1:{query}", download=False)
    entries = (info or {}).get("entries") or []
    if not entries:
        return None, None
    return entries[0].get("id"), entries[0].get("title") or "that video"


def play_youtube(query: str) -> str:
    """Resolve a real video and open its watch URL (auto-plays) in the default browser. Honest on
    every failure path — never claims playback without a resolved video AND a real launch."""
    import random
    q = (query or "").strip().strip(".!?,")
    q = re.sub(r"\s+on\s+(?:youtube|yt)\s*$", "", q, flags=re.I).strip()
    if not q or q.lower() in _PLAY_BARE:
        q = random.choice(_MUSIC_QUERIES)
    try:
        vid, title = _resolve_youtube(q)
    except Exception as e:
        print(f"[computer play resolve failed] {repr(e)[:100]}", file=sys.stderr)
        vid, title = None, None
    if not vid:
        return f"I couldn't pull up a video for \"{q}\" — YouTube search isn't answering right now."
    r = open_url(f"https://www.youtube.com/watch?v={vid}")
    if r.lower().startswith("opened"):
        _audit("PLAY", f"{q}\t{vid}\t{title}")
        return f"Playing \"{title}\" on YouTube."
    return r   # the launch itself failed — open_url's honest report stands


def _gap_phrase(low: str) -> str:
    """A short, honest name for a request that's OUTSIDE the five named actions, used in the
    up-front refusal ('I can't <gap> yet — ...'). Never an offer to try."""
    if re.search(r"\b(send|e-?mail)\b", low):
        return "send email"
    if re.search(r"\b(post|tweet|publish|dm)\b", low):
        return "post or message for you"
    if re.search(r"\b(buy|purchase|order|pay|checkout|transfer|venmo)\b", low):
        return "buy things or move money"
    if re.search(r"\b(delete|remove|erase|wipe|format|trash|empty|move|rename|overwrite)\b", low):
        return "delete or move files"
    if re.search(r"\b(uninstall|install|update\s+windows)\b", low):
        return "install or remove software"
    if re.search(r"\b(close|quit|kill|terminate|end\s+task)\b", low):
        return "close apps"
    if re.search(r"\b(shut\s*down|shutdown|restart|reboot|log\s*off|sign\s*out)\b", low):
        return "shut down or restart the PC"
    if re.search(r"\b(settings|preferences|disable|enable|registry)\b", low):
        return "change system settings"
    if re.search(r"\b(password|credential|api\s*key|login)\b", low):
        return "handle your credentials"
    if re.search(r"\b(rm|rmdir|del|sudo|powershell|terminal|command\s*prompt|cmd|bash|sh|exec|script)\b", low):
        return "run shell commands"
    return "do that"


def interpret(message: str) -> Decision:
    """Map a natural-language control request to a Decision. Deterministic and fail-safe: anything
    not clearly on the SAFE list is RISKY. For requests OUTSIDE the five named actions, reason holds
    a complete, honest refusal sentence (handle_control returns it verbatim — no offer to try)."""
    msg = (message or "").strip()
    low = msg.lower()

    # 1. screenshot — always safe (reads the screen, changes nothing)
    if _SCREENSHOT.search(low):
        return Decision("screenshot", "", "safe", True, "", msg)

    # 2. list windows — safe (reads window titles). Patterns are specific (list/show + windows|apps,
    #    or what + running|open) so "open notepad" doesn't match; checked before the open verb so
    #    "list my open windows" reads as a query, not an open.
    if _LIST_WINDOWS.search(low):
        return Decision("list_windows", "", "safe", True, "", msg)

    # 3. destructive / system / send / money words -> NOT one of the five actions. Unsupported:
    #    one honest up-front refusal (reason is the full sentence), never a fake offer-to-try.
    if _RISKY_WORDS.search(low):
        return Decision(None, msg, "risky", False,
                        f"I can't {_gap_phrase(low)} yet — right now {_CAPS}.", msg)

    # 4. play <something> — resolve a real YouTube video and open it (auto-plays). SAFE: composes
    #    the open_url action with a read-only search. Bare "play it/music/something" (the follow-up
    #    after opening YouTube) plays a music pick instead of misreading "it" as an unknown app.
    m = _PLAY_RE.match(low)
    if m:
        return Decision("play_youtube", _clean_target(m.group(1)), "safe", True, "", msg)

    # 5. switch/focus a window — safe (just brings an existing window forward)
    m = re.search(rf"(?:{_SWITCH_VERBS})\s+(?:the\s+|to\s+)?(.+)", low)
    if m:
        return Decision("focus_window", _clean_target(m.group(1)), "safe", True, "", msg)

    # 5. open/launch — a URL/site is safe; a whitelisted app is safe; an unknown app is RISKY
    m = re.search(rf"(?:{_OPEN_VERBS})\s+(.+)", low, re.I)
    if m:
        target = _clean_target(m.group(1))
        # "open <site> in <browser>" — opening a URL in a (named) browser is SAFE, no confirmation.
        # This is the case that used to be misread as an unknown app and falsely confirmed.
        site_part, browser = _split_browser(target)
        if browser:
            return Decision("open_url", _to_url(site_part), "safe", True, "", msg, browser=browser)
        url = _looks_like_url(target)
        if url:
            return Decision("open_url", url, "safe", True, "", msg)
        if target.lower() in WHITELIST:
            return Decision("open_app", target.lower(), "safe", True, "", msg)
        # an app we don't know — doable, but only after a yes
        return Decision("open_app", target, "risky", True,
                        f"\"{target}\" isn't on your safe-apps list", msg)

    # 6. routed to control but unparseable -> a clarifying question, not a fake offer-to-try
    return Decision(None, msg, "risky", False,
                    f"I'm not sure what you'd like me to do on your computer — right now {_CAPS}. "
                    "What are you after?", msg)


# ---------------------------------------------------------------------------
# The five named actions (the entire physical capability surface)
# ---------------------------------------------------------------------------

def _spawn(exe: str, *args: str) -> bool:
    """Launch a verified exe with args, no shell. Returns True only if the process actually
    started (Popen raises on a bad path) — so callers NEVER claim success without a real launch."""
    try:
        subprocess.Popen([exe, *args], close_fds=True)
        return True
    except Exception as e:
        print(f"[computer launch failed] {exe}: {repr(e)[:80]}", file=sys.stderr)
        return False


def _running(basename: str, tries: int = 3, gap: float = 0.3) -> bool:
    """Poll tasklist for a process basename (e.g. 'outlook.exe'), briefly, so a launch can be
    CONFIRMED rather than assumed. Blocks up to ~tries*gap seconds; fine for a deliberate open."""
    for i in range(tries):
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {basename}", "/NH"],
                                 capture_output=True, text=True, timeout=4).stdout.lower()
            if basename.lower() in out:
                return True
        except Exception:
            return False
        if i < tries - 1:
            time.sleep(gap)
    return False


def _launch(target: str) -> str:
    """Start an app without a shell and report what actually happened — never optimistically.
    Returns 'ok' (process confirmed running via tasklist), 'unverified' (we issued the launch but
    can't confirm a process appeared), or 'fail' (the launch raised). target is whitelisted or
    charset-validated by the caller."""
    basename = target if target.lower().endswith(".exe") else target + ".exe"
    exe = shutil.which(target) or shutil.which(target + ".exe") or _app_paths_lookup(basename)
    if exe and os.path.isfile(exe):
        if not _spawn(exe):
            return "fail"
        return "ok" if _running(os.path.basename(exe)) else "unverified"
    # couldn't resolve a real exe — best-effort `start` (it resolves App Paths itself), then
    # confirm via tasklist. If nothing shows up, stay honest: 'unverified', not a fake success.
    try:
        subprocess.Popen(["cmd", "/c", "start", "", target], close_fds=True)
    except Exception as e:
        print(f"[computer launch failed] start {target}: {repr(e)[:80]}", file=sys.stderr)
        return "fail"
    return "ok" if _running(basename) else "unverified"


def open_app(name: str) -> str:
    spec = WHITELIST.get(name.lower(), name)
    if not _SAFE_NAME.match(spec):
        return f"I won't launch \"{name}\" — the name has characters I don't allow."
    status = _launch(spec)
    if status == "ok":
        return f"Opened {name} for you."
    if status == "unverified":
        return (f"I tried to open {name} but couldn't confirm it actually started — "
                "if it didn't pop up, it may not be installed here.")
    return f"I tried to open {name} but it didn't start — it may not be installed."


def open_url(url: str, browser: str | None = None) -> str:
    """Open a URL. A NAMED browser is honored exactly or reported missing (never silently
    swapped). The default opens BROWSER, then falls back Brave -> Chrome -> OS default (logged).
    Every return reflects whether a launch actually happened."""
    if not re.match(r"^https?://", url):
        url = "https://" + url

    # A specifically named browser: open it or say plainly it's not installed — never substitute.
    if browser:
        exe, canon = _browser_exe(browser)
        if exe is None:
            _audit("BROWSER-MISSING", f"{browser}\t{url}")
            return f"I didn't open it — {canon or browser} doesn't look installed on this machine."
        if _spawn(exe, url):
            _audit("BROWSER", f"{canon}\t{exe}\t{url}")
            return f"Opened {url} in {canon.capitalize()}."
        return f"I tried to open {url} in {canon.capitalize()} but it didn't start."

    # Default: BROWSER first, then Brave -> Chrome, then the OS default.
    for cand in dict.fromkeys([BROWSER, "brave", "chrome"]):   # ordered, de-duped
        exe, canon = _browser_exe(cand)
        if exe and _spawn(exe, url):
            note = "" if cand == BROWSER else f" ({BROWSER.capitalize()} wasn't found)"
            _audit("BROWSER", f"{canon}\t{exe}\t{url}{'  fallback' if cand != BROWSER else ''}")
            if cand != BROWSER:
                print(f"[computer] {BROWSER} not found — opened URL in {canon}", file=sys.stderr)
            return f"Opened {url} in {canon.capitalize()}{note}."
    # Last resort: whatever the OS has set as default.
    try:
        os.startfile(url)
        _audit("BROWSER", f"os-default\t{url}")
        print(f"[computer] no Brave/Chrome found — opened URL in the OS default browser", file=sys.stderr)
        return f"Opened {url} in your default browser."
    except Exception as e:
        return f"I couldn't open {url} ({repr(e)[:60]})."


def screenshot() -> str:
    try:
        from PIL import ImageGrab
        _SHOTS.mkdir(exist_ok=True)
        path = _SHOTS / f"shot-{datetime.datetime.now():%Y%m%d-%H%M%S}.png"
        ImageGrab.grab().save(path)
        return f"Took a screenshot — saved it to {path}."
    except Exception as e:
        return f"I couldn't take a screenshot ({repr(e)[:60]})."


def list_windows() -> str:
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        titles: list[str] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    t = buf.value.strip()
                    if t:
                        titles.append(t)
            return True

        u.EnumWindows(EnumProc(cb), 0)
        seen, uniq = set(), []
        for t in titles:
            if t not in seen:
                seen.add(t); uniq.append(t)
        if not uniq:
            return "I don't see any titled windows open."
        shown = uniq[:12]
        return "Here's what's open: " + ", ".join(shown) + ("…" if len(uniq) > 12 else "") + "."
    except Exception as e:
        return f"I couldn't list the windows ({repr(e)[:60]})."


def focus_window(substr: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        target = (substr or "").lower()
        found = {"hwnd": None, "title": None}
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    t = buf.value
                    if target in t.lower() and found["hwnd"] is None:
                        found["hwnd"], found["title"] = hwnd, t
            return True

        u.EnumWindows(EnumProc(cb), 0)
        if found["hwnd"] is None:
            return f"I don't see a window matching \"{substr}\" open right now."
        u.ShowWindow(found["hwnd"], 9)        # SW_RESTORE
        u.SetForegroundWindow(found["hwnd"])
        return f"Switched to {found['title']}."
    except Exception as e:
        return f"I couldn't switch windows ({repr(e)[:60]})."


_ACTIONS = {"open_app": lambda d: open_app(d.target),
            "open_url": lambda d: open_url(d.target, d.browser),
            "play_youtube": lambda d: play_youtube(d.target),
            "screenshot": lambda d: screenshot(),
            "list_windows": lambda d: list_windows(),
            "focus_window": lambda d: focus_window(d.target)}


def _execute(d: Decision) -> str:
    fn = _ACTIONS.get(d.action)
    if not fn:
        return "I can only open apps and websites, take screenshots, and list or switch windows."
    result = fn(d)
    _audit("EXEC", f"{d.action}\t{d.target}\t{result}")
    return result


# ---------------------------------------------------------------------------
# Pending-confirmation gate (per user; single-user, single-process — see scope note)
# ---------------------------------------------------------------------------

_pending: dict[str, tuple[Decision, float]] = {}


def handle_control(user_id: str, message: str) -> str:
    """Entry point for the 'control' route. SAFE -> execute now. RISKY-but-doable (a non-whitelisted
    app) -> ask once and wait for a yes. UNSUPPORTED (outside the five actions: send/delete/close/
    shell/etc.) -> ONE honest refusal up front; never a fake offer-to-try that admits defeat later."""
    d = interpret(message)
    if d.risk == "safe":
        return _execute(d)

    if not d.supported:
        # outside the action list entirely — refuse honestly now, store nothing, ask nothing
        _audit("REFUSE", str(d.target)[:80])
        return d.reason

    # risky but genuinely doable (only the non-whitelisted-app case reaches here) — confirm first
    _pending[user_id] = (d, time.time())
    _audit("ASK", f"open non-whitelisted app\t{d.target}")
    return f"{d.reason}. Want me to open it anyway? Say yes and I will, or no to skip it."


def resolve_pending(user_id: str, message: str) -> str | None:
    """Called BEFORE routing every turn. A pending ask is ONLY ever a supported, risky action (a
    non-whitelisted app open) — unsupported requests are refused up front and never stored. A yes
    executes it, a no cancels, anything else abandons it and returns None so the new message routes
    normally. Returns a reply string when it handled the turn, else None."""
    item = _pending.get(user_id)
    if not item:
        return None
    d, ts = item
    if time.time() - ts > _PENDING_TTL:        # stale ask — drop it, route normally
        _pending.pop(user_id, None)
        return None

    if _AFFIRM.match(message or ""):
        _pending.pop(user_id, None)
        _audit("CONFIRM-EXEC", f"{d.action}\t{d.target}")
        return _execute(d)

    if _DENY.match(message or ""):
        _pending.pop(user_id, None)
        _audit("CANCEL", f"{d.target}")
        return "Okay, leaving it."

    # neither yes nor no — they moved on; drop the pending ask and let the turn route normally
    _pending.pop(user_id, None)
    _audit("ABANDON", f"{d.target}")
    return None


def clear_pending(user_id: str) -> None:
    _pending.pop(user_id, None)
