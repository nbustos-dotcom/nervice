"""ONE FRONT DOOR: the LLM router is the single intent decider. It returns a route PLUS extracted
arguments; deterministic code only EXECUTES (risk gating, whitelists, and the pending-confirm gate
in app/computer.py are unchanged). The old deterministic doormen (music regexes, the sysinfo
guard) no longer pre-empt intent — they are route targets.

classify() always returns a dict: {"route": <name>, ...args}. Fallback ladder unchanged:
Groq chat_json -> local Ollama chat_json -> deterministic keyword net (so machine control never
silently dies with every model rung down)."""
import re
import sys

from app.llm import chat_json
from app import computer   # leaf module — its verb/risk vocabularies are the single source of truth
from app import skills     # current trigger list is injected into the router prompt each call
from app import ollama_client as ollama   # local classify rung when Groq is capped

ROUTER_SYSTEM = """Classify the user's message into exactly one route and extract its arguments. Output ONLY JSON, no prose.

ROUTES + their JSON shapes:
{"route":"control","action":"open_app|open_url|play_youtube|screenshot|focus_window|list_windows","target":"<app name | site/url | search query | window name | empty>"}
  control = DO something on Nate's computer. open_app: launch a desktop app ("open notepad" -> action open_app, target "notepad"). open_url: open a website ("open youtube", "pull up reddit"). play_youtube: play music/a song/a video ("play some music" -> target ""; "play bohemian rhapsody" -> target "bohemian rhapsody"; "actually play it" -> target ""). screenshot. focus_window: switch to a window. list_windows: what's open.
  A destructive/system request (close/delete/uninstall/send/settings/shell) is ALSO control but with NO action field — the executor refuses it honestly.
{"route":"skill","op":"run|create|list|delete","trigger":"<trigger phrase or empty>"}
  skill = the user's SAVED shortcuts. op run when the message matches a SAVED TRIGGER (listed below). op create when teaching a new one ("when I say X, do Y", "make a skill that..."). op list ("list my skills"). op delete ("delete the X skill").
{"route":"music_mgmt","op":"set|add|remove|list|clear","artists":["..."]}
  music_mgmt = managing Nate's favorite-artists list: "my favorite artists are X, Y, Z" (set), "add X to my artists" (add), "remove X" (remove), "who are my favorite artists" (list), "clear my artists" (clear). PLAYING music is control/play_youtube, not music_mgmt.
{"route":"system","question":"cpu|ram|gpu|disk|os|uptime|specs|top_proc|file_count","path":"<folder for file_count, or empty>"}
  system = READ-ONLY questions about THIS machine: "what CPU do I have" (cpu), "how much RAM" (ram), "how much disk space" (disk), "what's using the most memory" (top_proc), "how many files in my Downloads" (file_count, path "Downloads"), "how many files can you read in my computer" (file_count, path "" — "my computer"/"the computer" is NOT a folder path, leave path empty), "what OS" (os), "how long has it been up" (uptime), "what are my specs" (specs).
{"route":"hard"}   hard = formal logic puzzles/proofs/multi-step quantitative problems; nontrivial architecture/schema design or review; long rigorous analysis where wrong answers are costly; or the user explicitly asks for Claude.
{"route":"build"}  build = an explicit request to create/edit/fix actual FILES or projects. Discussing code is normal.
{"route":"selfmod"} selfmod = an explicit request to change Nervice's OWN behavior/personality/code ("stop ending sentences with questions"). Opinions about itself are normal.
{"route":"browse"} browse = find/check/read/report something ON a specific named website ("open hacker news and tell me the top story"). General factual/news questions are normal.
{"route":"normal"} normal = everything else: chat, opinions, simple facts, news/current events, and any DISCUSSION (vs an explicit action request).

RULES:
- A REACTION/FOLLOW-UP about something that just happened ("it didn't open", "that didn't work", "I don't see it") is ALWAYS normal.
- An explicit action request beats discussion; discussion routes normal.
- If the message exactly matches a SAVED TRIGGER below, route skill/run with that trigger.
- Asking ABOUT capabilities ("can you play music?") is normal, not control.
- Extract targets/queries minimally and literally; strip polite prefixes ("Jarvis,", "please")."""

_ROUTES = {"hard", "build", "selfmod", "browse", "control", "skill", "music_mgmt", "system", "normal"}
_CTRL_ACTIONS = {"open_app", "open_url", "play_youtube", "screenshot", "focus_window", "list_windows"}
_SKILL_OPS = {"run", "create", "list", "delete"}
_MUSIC_OPS = {"set", "add", "remove", "list", "clear"}
_SYS_QUESTIONS = {"cpu", "ram", "gpu", "disk", "os", "uptime", "specs", "top_proc", "file_count"}

# Deterministic guard: a complaint/reaction about a prior action must stay conversational and NEVER
# reach the browse agent or the control interpreter, regardless of what the LLM router decides.
_FOLLOWUP = re.compile(
    r"\b(did(n'?t| not)\s+(appear|open|work|show|launch|come up|do anything|pop up)|"
    r"not\s+(showing|there|appearing|visible|working|here)|"
    r"don'?t\s+see|can'?t\s+see\s+(it|anything|that)|i\s+(want to|wanna)\s+see\s+it|"
    r"where('?s| is| did)\s+it|nothing\s+(happened|appeared|opened|showed)|"
    r"it'?s\s+not\s+(here|showing|there|working|open|up))\b", re.I)

# Vocabulary for the no-LLM keyword net ONLY (both model rungs down).
_SYSINFO_NOUN = re.compile(r"\b(cpu|gpu|graphics card|processor|cores?|ram|memory|disk|storage|drive|"
                           r"space|specs?|hardware|uptime|operating system|\bos\b|processes?|files?|folders?)\b", re.I)
_SYSINFO_ASK = re.compile(r"\b(what|which|how\s+(?:much|many|big|busy)|do\s+i\s+have|how'?s|"
                          r"tell me|show me|using the most|what'?s\s+(?:using|running|eating))\b", re.I)
_MACHINE_ACTION = re.compile(r"\b(open|launch|start|close|quit|kill|terminate|end|stop|delete|remove|"
                             r"erase|wipe|empty|clear|format|trash|move|rename|install|uninstall|"
                             r"screenshot|switch|focus|run|create|make|shut\s*down|shutdown|reboot|restart)\b", re.I)


def is_machine_question(msg: str) -> bool:
    """Deterministic: a READ-ONLY machine question (no action verb). Keyword-net use only."""
    m = msg or ""
    return bool(_SYSINFO_ASK.search(m) and _SYSINFO_NOUN.search(m) and not _MACHINE_ACTION.search(m))


_KW_OPEN = re.compile(rf"^\s*(?:please\s+|hey\s+|can you\s+)?(?:{computer._OPEN_VERBS})\s+\S", re.I)
_KW_SWITCH = re.compile(rf"\b(?:{computer._SWITCH_VERBS})\b", re.I)


def _keyword_route(msg: str) -> dict:
    """Deterministic final net when BOTH LLM routers are down. Returns route dicts without rich
    args — executors fall back to parsing the raw message (computer.interpret / sysinfo's text
    dispatcher). Machine control NEVER silently dies."""
    m = msg or ""
    if is_machine_question(m):
        return {"route": "system"}                    # executor parses the question from raw text
    if computer._SCREENSHOT.search(m) or computer._LIST_WINDOWS.search(m) or _KW_SWITCH.search(m):
        return {"route": "control"}
    if computer._RISKY_WORDS.search(m):               # delete/uninstall/send/... -> honest refusal
        return {"route": "control"}
    if _KW_OPEN.search(m):
        return {"route": "control"}
    if computer._PLAY_RE.match(m):
        return {"route": "control"}
    return {"route": "normal"}


def _coerce(out) -> dict | None:
    """Validate + normalize the router's JSON into a safe route dict. None if unusable."""
    if not isinstance(out, dict):
        return None
    route = out.get("route")
    if route not in _ROUTES:
        return None
    d = {"route": route}
    if route == "control":
        a = str(out.get("action") or "").strip().lower()
        if a in _CTRL_ACTIONS:
            d["action"] = a
            d["target"] = str(out.get("target") or "").strip()
        # no/invalid action -> executor runs interpret(raw) and refuses honestly if unsupported
    elif route == "skill":
        op = str(out.get("op") or "run").strip().lower()
        d["op"] = op if op in _SKILL_OPS else "run"
        d["trigger"] = str(out.get("trigger") or "").strip()
    elif route == "music_mgmt":
        op = str(out.get("op") or "").strip().lower()
        if op not in _MUSIC_OPS:
            return None                                # unusable music op -> let caller fall back
        d["op"] = op
        arts = out.get("artists") or []
        d["artists"] = [str(a).strip() for a in arts if str(a).strip()] if isinstance(arts, list) else []
    elif route == "system":
        q = str(out.get("question") or "").strip().lower()
        d["question"] = q if q in _SYS_QUESTIONS else None
        d["path"] = str(out.get("path") or "").strip()
    return d


def _prompt_with_triggers() -> str:
    try:
        triggers = [s.get("trigger", "") for s in skills.load_skills() if s.get("trigger")]
    except Exception:
        triggers = []
    return ROUTER_SYSTEM + "\n\nSAVED TRIGGERS: " + (", ".join(f'"{t}"' for t in triggers) if triggers else "(none)")


async def classify(user_message: str) -> dict:
    """The single intent decider. Always returns {"route": <name>, ...extracted args}."""
    msg = user_message or ""
    # A reaction/complaint about a prior action stays conversational regardless of the LLM.
    if _FOLLOWUP.search(msg):
        return {"route": "normal"}
    system = _prompt_with_triggers()
    try:
        d = _coerce(await chat_json(system, msg))
        if d:
            return d
        return {"route": "normal"}
    except Exception:
        # Groq router unavailable. Local Ollama next (same prompt, same schema), keywords last.
        try:
            d = _coerce(await ollama.chat_json(system, msg))
            if d:
                print(f"[router fallback] groq down -> ollama route '{d['route']}'", file=sys.stderr)
                return d
        except Exception:
            pass
        r = _keyword_route(msg)
        print(f"[router fallback] llm+ollama unavailable -> keyword route '{r['route']}'", file=sys.stderr)
        return r