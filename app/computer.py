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
    def __init__(self, action, target, risk, supported, reason, raw):
        self.action = action          # 'open_app'|'open_url'|'screenshot'|'list_windows'|'focus_window'|None
        self.target = target          # app name / url / window substring / ''
        self.risk = risk              # 'safe' | 'risky'
        self.supported = supported    # can we actually do it (False for delete/close/send/etc.)
        self.reason = reason          # why risky, for the spoken confirmation
        self.raw = raw                # original message


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


def interpret(message: str) -> Decision:
    """Map a natural-language control request to a Decision. Deterministic and fail-safe: anything
    not clearly on the SAFE list is RISKY."""
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

    # 3. destructive / system / send / money words -> RISKY, and NOT in our action list (unsupported)
    rw = _RISKY_WORDS.search(low)
    if rw:
        verb = rw.group(0).lower()
        return Decision(None, msg, "risky", False,
                        f"that would {verb} something on your machine, which I don't do without a clear yes",
                        msg)

    # 4. switch/focus a window — safe (just brings an existing window forward)
    m = re.search(rf"(?:{_SWITCH_VERBS})\s+(?:the\s+|to\s+)?(.+)", low)
    if m:
        return Decision("focus_window", _clean_target(m.group(1)), "safe", True, "", msg)

    # 5. open/launch — a URL/site is safe; a whitelisted app is safe; an unknown app is RISKY
    m = re.search(rf"(?:{_OPEN_VERBS})\s+(.+)", low, re.I)
    if m:
        target = _clean_target(m.group(1))
        url = _looks_like_url(target)
        if url:
            return Decision("open_url", url, "safe", True, "", msg)
        if target.lower() in WHITELIST:
            return Decision("open_app", target.lower(), "safe", True, "", msg)
        # an app we don't know — doable, but only after a yes
        return Decision("open_app", target, "risky", True,
                        f"\"{target}\" isn't on your safe-apps list", msg)

    # 6. routed to control but unparseable -> fail safe: ask
    return Decision(None, msg, "risky", False,
                    "I'm not sure exactly what you want me to do on your computer", msg)


# ---------------------------------------------------------------------------
# The five named actions (the entire physical capability surface)
# ---------------------------------------------------------------------------

def _launch(target: str) -> None:
    """Start an app without a shell. Resolve a real exe on PATH first; otherwise hand the (already
    sanitized / whitelisted) name to Windows `start`, which resolves App Paths (chrome, spotify,
    code). shell=False throughout and the target is never raw user text with metachars."""
    exe = shutil.which(target) or shutil.which(target + ".exe")
    if exe and exe.lower().endswith(".exe"):
        subprocess.Popen([exe], close_fds=True)
        return
    # App-Paths / .cmd shims (code, chrome, spotify): `cmd /c start "" <target>`; target is a single
    # argv element (no shell parsing) and is whitelisted or charset-validated by the caller.
    subprocess.Popen(["cmd", "/c", "start", "", target], close_fds=True)


def open_app(name: str) -> str:
    spec = WHITELIST.get(name.lower(), name)
    if not _SAFE_NAME.match(spec):
        return f"I won't launch \"{name}\" — the name has characters I don't allow."
    try:
        _launch(spec)
        return f"Opened {name} for you."
    except Exception as e:
        return f"I tried to open {name} but it didn't start ({repr(e)[:60]})."


def open_url(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        os.startfile(url)   # default browser, no shell
        return f"Opened {url} in your browser."
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
            "open_url": lambda d: open_url(d.target),
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
    """Entry point for the 'control' route. SAFE -> execute now. RISKY -> store a pending
    confirmation and ASK; nothing risky runs until resolve_pending() sees a yes."""
    d = interpret(message)
    if d.risk == "safe":
        return _execute(d)

    # RISKY: never execute now — record the ask and wait for confirmation
    _pending[user_id] = (d, time.time())
    if d.supported and d.action == "open_app":
        _audit("ASK", f"open non-whitelisted app\t{d.target}")
        return (f"{d.reason}. Want me to open it anyway? Say yes and I will, "
                "or no to skip it.")
    _audit("ASK", f"risky/unsupported\t{d.target}")
    return (f"Hold on — {d.reason}. I won't do that without you confirming. "
            "Say yes if you really want me to try, or no to leave it.")


def resolve_pending(user_id: str, message: str) -> str | None:
    """Called BEFORE routing every turn. If a confirmation is pending: a yes executes it (if it's
    actually doable), a no cancels, anything else abandons it and returns None so the new message
    routes normally. Returns a reply string when it handled the turn, else None."""
    item = _pending.get(user_id)
    if not item:
        return None
    d, ts = item
    if time.time() - ts > _PENDING_TTL:        # stale ask — drop it, route normally
        _pending.pop(user_id, None)
        return None

    if _AFFIRM.match(message or ""):
        _pending.pop(user_id, None)
        if not d.supported:
            _audit("CONFIRM-UNSUPPORTED", f"{d.target}")
            return ("Even with your okay, I genuinely can't do that — my control here is limited to "
                    "opening apps and websites, taking screenshots, and switching windows. "
                    "So I've left everything alone.")
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
