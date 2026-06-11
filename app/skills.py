"""User-defined skills (shortcuts): a TRIGGER phrase that runs an ordered list of actions Nervice
ALREADY HAS. A skill can ONLY compose existing capabilities — open_app, open_url, play-on-youtube,
weather, news — it can never invent a new capability, run a shell, or bypass the safe-action
boundary / risky-action gating in app.computer. Non-whitelisted-app and unsupported steps are
flagged at creation and never auto-run.

Detection is deterministic and runs BEFORE normal routing (called from respond()/stream_reply(),
right after the computer pending-confirmation gate). Store: data/skills.json (gitignored, persisted).
"""
import re
import json
import time
import pathlib
import datetime

from app import computer

_STORE = pathlib.Path(__file__).resolve().parent.parent / "data" / "skills.json"

# A skill step is ONE of these existing safe capabilities. There is no freeform action — anything
# else a user asks for in a skill is reported as unsupported, never invented.
STEP_ACTIONS = {"open_app", "open_url", "youtube", "weather", "news"}

_pending_skill: dict[str, tuple[dict, float]] = {}   # user_id -> (skill, ts) awaiting a save yes/no
_PENDING_TTL = 300


# ----------------------------- store -----------------------------
def load_skills() -> list:
    try:
        if _STORE.exists():
            d = json.loads(_STORE.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []


def _save(skills: list) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(skills, indent=2), encoding="utf-8")
    except Exception:
        pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def find_skill(text: str):
    """Match an utterance to a saved trigger: the utterance IS the trigger (normalized), or
    'run/do/start/activate <trigger>'. Exact match only, so normal chat doesn't false-trigger."""
    skills = load_skills()
    nt = _norm(text)
    if not nt:
        return None
    for sk in skills:
        if _norm(sk.get("trigger", "")) == nt:
            return sk
    m = re.match(r"(?:run|do|start|activate|trigger|execute)\s+(?:the\s+)?(.+?)(?:\s+skill|\s+shortcut)?$", nt)
    if m:
        tgt = _norm(m.group(1))
        for sk in skills:
            if _norm(sk.get("trigger", "")) == tgt:
                return sk
    return None


# ----------------------------- labels -----------------------------
def _step_label(s: dict) -> str:
    a, arg = s.get("action"), (s.get("arg") or "")
    if a == "open_app":
        return f"open {arg}"
    if a == "open_url":
        return f"open {arg}"
    if a == "youtube":
        return f"play \"{arg}\" on YouTube" if arg else "open YouTube"
    if a == "weather":
        return "tell the weather"
    if a == "news":
        return "tell the news"
    return f"{a} {arg}".strip()


# ----------------------------- run (same safe actions + same gating) -----------------------------
async def _run_step(step: dict) -> str:
    """Execute ONE step via the existing safe capability. Honors the computer.WHITELIST boundary:
    a non-whitelisted app is risky and is NOT auto-opened (it's reported, not run) — skills can't
    bypass the gate. Returns a short spoken piece."""
    a, arg = step.get("action"), (step.get("arg") or "").strip()
    if a == "open_app":
        spec = computer.WHITELIST.get(arg.lower(), arg)
        if not computer._SAFE_NAME.match(spec):
            return f"skipped {arg} (unsafe name)"
        if arg.lower() not in computer.WHITELIST:
            return f"skipped opening {arg} — not on your safe-apps list, so it needs a yes each time"
        computer._audit("SKILL-STEP", f"open_app\t{arg}")
        r = computer.open_app(arg)
        return r if r.lower().startswith(("opened", "i tried")) else f"opened {arg}"
    if a == "open_url":
        url = computer._looks_like_url(arg) or ("https://" + re.sub(r"\s+", "", arg)) if arg else "https://www.google.com"
        computer._audit("SKILL-STEP", f"open_url\t{url}")
        return computer.open_url(url)
    if a == "youtube":
        url = ("https://www.youtube.com/results?search_query=" + re.sub(r"\s+", "+", arg.strip())) if arg else "https://www.youtube.com"
        computer._audit("SKILL-STEP", f"youtube\t{arg}")
        computer.open_url(url)
        return f"opened YouTube for {arg}" if arg else "opened YouTube"
    if a == "weather":
        from app.weather import get_weather
        w = await get_weather()
        return w or "couldn't get the weather"
    if a == "news":
        try:
            from app.api import _fetch_news
            items = await _fetch_news()
        except Exception:
            items = []
        return ("here's the latest — " + "; ".join(i["title"] for i in items[:3])) if items else "no headlines right now"
    return f"skipped an unknown step ({a})"


async def run_skill(user_id: str, sk: dict) -> str:
    pieces = []
    for step in sk.get("steps", []):
        try:
            pieces.append(await _run_step(step))
        except Exception as e:
            pieces.append(f"a step failed ({repr(e)[:40]})")
    computer._audit("SKILL-RUN", f"{sk.get('trigger')}\t{len(sk.get('steps', []))} steps")
    body = "; ".join(p for p in pieces if p)
    return f"Running {sk.get('trigger')}: {body}." if body else f"Ran {sk.get('trigger')}, but it had no steps."


# ----------------------------- create (parse -> confirm -> save) -----------------------------
_PARSE_SYSTEM = """Extract a user-defined skill from the request. A skill = a TRIGGER phrase + an
ORDERED list of steps. Each step MUST be exactly one of these existing actions (never invent others):
- open_app  (arg = desktop app name, e.g. "vscode", "notepad", "spotify", "chrome")
- open_url  (arg = website name or url, e.g. "github", "youtube.com", "reddit")
- youtube   (arg = what to search/play on YouTube)
- weather   (no arg)
- news      (no arg)
If a requested step is NOT one of those (close/quit an app, delete/move files, send a message,
buy something, change settings, run a command, or anything destructive), DO NOT invent an action —
put a short human description of it in "unsupported".
Output ONLY JSON, no prose:
{"trigger":"work mode","steps":[{"action":"open_app","arg":"vscode"},{"action":"open_url","arg":"github"}],"unsupported":[]}
The trigger is the short phrase the user will say to run it. Keep args short and literal."""


async def _create(user_id: str, text: str) -> str:
    from app.llm import chat_json
    try:
        parsed = await chat_json(_PARSE_SYSTEM, text)
    except Exception:
        return "I couldn't parse that into a skill — try \"when I say work mode, open vscode and open github\"."
    trigger = (parsed.get("trigger") or "").strip()
    unsupported = [str(u) for u in (parsed.get("unsupported") or [])]
    steps, notes = [], []
    for s in (parsed.get("steps") or []):
        a = (s.get("action") or "").strip().lower()
        arg = (s.get("arg") or "").strip()
        if a not in STEP_ACTIONS:                       # never invent a capability
            unsupported.append(f"{a} {arg}".strip())
            continue
        if a == "open_app" and arg.lower() not in computer.WHITELIST:
            notes.append(f"“{arg}” isn't on your safe-apps list — I'll skip it at run unless you add it")
        steps.append({"action": a, "arg": arg})
    if not trigger or not steps:
        msg = "I couldn't make a skill from that"
        if unsupported:
            msg += f" — skills only chain open-app, open-site, YouTube, weather, and news; I can't: {', '.join(unsupported)}"
        return msg + "."
    sk = {"trigger": trigger, "steps": steps, "created": datetime.datetime.now().date().isoformat()}
    _pending_skill[user_id] = (sk, time.time())
    seq = "; ".join(f"{i+1}) {_step_label(s)}" for i, s in enumerate(steps))
    extra = ""
    if unsupported:
        extra += f" I left out (can't do): {', '.join(unsupported)}."
    if notes:
        extra += " " + "; ".join(notes) + "."
    return f"Got it — saying “{trigger}” will: {seq}.{extra} Save it?"


def _resolve_pending(user_id: str, text: str):
    item = _pending_skill.get(user_id)
    if not item:
        return None
    sk, ts = item
    if time.time() - ts > _PENDING_TTL:
        _pending_skill.pop(user_id, None)
        return None
    if computer._AFFIRM.match(text or ""):
        _pending_skill.pop(user_id, None)
        skills = [s for s in load_skills() if _norm(s.get("trigger", "")) != _norm(sk["trigger"])]
        skills.append(sk)
        _save(skills)
        computer._audit("SKILL-SAVE", f"{sk['trigger']}\t{len(sk['steps'])} steps")
        return f"Saved. Say “{sk['trigger']}” anytime to run it."
    if computer._DENY.match(text or ""):
        _pending_skill.pop(user_id, None)
        return "Okay, didn't save it."
    _pending_skill.pop(user_id, None)   # moved on — drop it, route the new message normally
    return None


# ----------------------------- list / delete -----------------------------
def _list_text() -> str:
    skills = load_skills()
    if not skills:
        return "You don't have any skills yet. Teach me one like: \"when I say work mode, open vscode and open github.\""
    lines = []
    for sk in skills:
        seq = ", ".join(_step_label(s) for s in sk.get("steps", []))
        lines.append(f"“{sk['trigger']}” → {seq}")
    return "Your skills: " + "; ".join(lines) + "."


def _delete(name_norm: str) -> str:
    skills = load_skills()
    keep = [s for s in skills if _norm(s.get("trigger", "")) != name_norm]
    if len(keep) == len(skills):
        return f"I don't have a skill called “{name_norm}”. Say \"list my skills\" to see them."
    _save(keep)
    computer._audit("SKILL-DELETE", name_norm)
    return f"Deleted the “{name_norm}” skill."


# ----------------------------- entry: deterministic pre-router gate -----------------------------
# Create needs BOTH a teach-cue AND an actual action the skill would perform — so normal talk
# ("add a skill to my resume", "create a macro in excel", "when I tell you to relax, do you?") can't
# hijack the turn or burn an LLM parse. 'command' is intentionally NOT a skill noun (too colliding).
_TEACH_CUE = re.compile(r"\bwhen i say\b|\b(make|create|add|set\s*up|teach\s+you|save|define)\b[^.]{0,18}\b(skill|shortcut|macro)\b", re.I)
_HAS_ACTION = re.compile(r"\b(opens?|launch(?:es)?|starts?|plays?|put(?:s)?\s+on|pull(?:s)?\s+up|"
                         r"bring(?:s)?\s+up|fire(?:s)?\s+up|go(?:es)?\s+to|tells?\s+me|shows?\s+me|"
                         r"gives?\s+me|gets?\s+me|check)\b", re.I)
_LIST_RE = re.compile(r"\b(my|your)\s+(skills?|shortcuts?)\b|\b(list|show)\b[^.]{0,12}\b(skills?|shortcuts?)\b|\bwhat\s+skills?\b", re.I)
_DELETE_RE = re.compile(r"\b(delete|remove|forget|drop|get rid of)\b\s+(?:the\s+|my\s+)?(.+?)\s*(?:skill|shortcut|macro)\b", re.I)


async def execute_op(user_id: str, op: str, trigger: str, raw_text: str) -> str:
    """Execute a structured skill operation from the ROUTER ({op, trigger}). The LLM decides the
    intent; this validates and runs through the existing (re-gated) machinery."""
    op = (op or "run").strip().lower()
    if op == "create":
        return await _create(user_id, raw_text)
    if op == "list":
        return _list_text()
    if op == "delete":
        name = _norm(trigger)
        if not name:
            m = _DELETE_RE.search((raw_text or "").lower())
            name = _norm(m.group(2)) if m else ""
        if not name:
            return "Which skill should I delete? Say \"delete the <name> skill.\""
        return _delete(name)
    # run
    sk = find_skill(trigger) or find_skill(raw_text)
    if sk:
        return await run_skill(user_id, sk)
    shown = trigger or raw_text
    return f"I don't have a skill called “{shown}”. Say \"list my skills\" to see them."


async def handle(user_id: str, text: str):
    """Called before normal routing. Returns a reply string if this turn is a skill operation
    (save-confirm / create / list / delete / run a trigger), else None so the turn routes normally."""
    # 1. a pending save confirmation answers first
    r = _resolve_pending(user_id, text)
    if r is not None:
        return r
    low = (text or "").strip().lower()
    if not low:
        return None
    # 2. create (before run, so 'when I say work mode ...' isn't matched as the trigger). Needs both
    #    a teach-cue and an action, so normal conversation can't trigger a parse.
    if _TEACH_CUE.search(low) and _HAS_ACTION.search(low):
        return await _create(user_id, text)
    # 3. delete
    m = _DELETE_RE.search(low)
    if m:
        return _delete(_norm(m.group(2)))
    # 4. list
    if _LIST_RE.search(low):
        return _list_text()
    # 5. run a saved trigger
    sk = find_skill(text)
    if sk:
        return await run_skill(user_id, sk)
    return None
