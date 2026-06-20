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
  system = READ-ONLY questions about THIS machine: "what CPU do I have" (cpu), "how much RAM" (ram), "how much disk space" (disk), "what's using the most memory" (top_proc), "how many files in my Downloads" (file_count, path "Downloads"), "how many files can you read in my computer" (file_count, path "" — "my computer"/"the computer" is NOT a folder path, leave path empty), "what OS" (os), "how long has it been up" (uptime), "what are my specs" (specs). TELEMETRY VALUES ONLY — a question about Nervice's ABILITIES ("can you see my screen", "what can you do", "are you able to X", "can you read/access/control X") is NOT system; route it normal so the brain answers honestly. A specific DISPLAY value you don't track — screen resolution, refresh rate, brightness, screen size — is also NOT system; route it normal.
{"route":"hard"}   hard = formal logic puzzles/proofs/multi-step quantitative problems; nontrivial architecture/schema design or review; long rigorous analysis where wrong answers are costly; or the user explicitly asks for Claude.
{"route":"build"}  build = an explicit request to create/edit/fix actual FILES or projects. Discussing code is normal.
{"route":"selfmod"} selfmod = an explicit request to change Nervice's OWN behavior/personality/code ("stop ending sentences with questions"). Opinions about itself are normal.
{"route":"browse"} browse = find/check/read/report something ON a specific named website ("open hacker news and tell me the top story"). General factual questions are normal; current news/headlines is the news route.
{"route":"canvas"} canvas = a question about Nate's SCHOOL CANVAS — what's due, upcoming assignments, due dates, announcements, or grades ("what's due this week", "any new assignments", "check canvas", "what are my grades", "anything due on canvas"). This READS his live Canvas page.
{"route":"orchestrator","op":"new|edit|critique|plan|next|done|redo|status|gaps|summary|result|workspace"}
  orchestrator = managing Nate's CODING PROJECT PLAN (the project doc at docs/projects/ACTIVE.md). op new = "start a new project", "new project", "create a project", "set up a new project", "begin a new project" — BEGINS a guided setup where Nervice asks questions and writes the project doc for him (this is the META setup of the project doc, NOT writing code/files — that's build). op edit = an EDIT to the EXISTING project — "change the goal to X", "add a step for X", "add a requirement X", "remove step 2", "remove the X step", "reorder step 3 before step 1", "rewrite requirement 1 as X", "change step 2 to X" (editing the CURRENT project doc/plan — NOT starting a new one). op critique = "critique my project", "review my project doc", "find gaps in my plan". op plan = "plan my project", "break it into steps", "show the plan", "what's the plan". op next = "next step", "give me the next step", "what do I paste next", "what's the next prompt". op done = a COMMAND to mark the current step COMPLETE: "mark step done", "mark it done", "I finished that step", "that step's done". A QUESTION about completion is NOT op done — "is it done", "is step 2 done", "are we done", "did I finish", "what's left" are op status. op redo = "redo", "redo that step", "give me a different prompt for this step". op status = a READ-ONLY status read, NEVER a mutation: "project status", "how many steps left", "what's left", "is it done", "is step 2 done", "are we done". op gaps = "show gaps", "show me the critique". op summary = "how's my project", "how's the project going", "give me a project update", "where am I on the project", "what's the loop waiting on", "what did Claude Code return", "where's the loop", "what's the loop doing". op result = Nate is PASTING Claude Code's report/output back after running a step's prompt — cues like "here's what Claude Code said", "here's the result", "CC said:", "result:", "it finished, here's the output", usually followed by the pasted report text. Nervice reads the report and proposes the next move (mark done + next step, or add a fix/follow-up step). The report arrives TWO ways, BOTH op result: Nate PASTES it, OR — when a project workspace is set — Nate says "check the result" / "read the result file" / "did Claude Code finish" / "fetch the result" and Nervice reads the workspace's .nervice/result.json itself (then git-cross-checks it). op workspace = set or SHOW where the project's CODE lives for that file channel — "set the project workspace to <path>", "set my project folder to X", "where's the workspace", "show the project workspace". NOT a request to build (that's build) and NOT a status question (that's summary). This is about Nate's coding PROJECT plan — NOT Canvas (school) and NOT building files right now.
{"route":"actions"} actions = a question about what NERVICE has DONE / its own recent activity ("what have you done", "what actions have you taken", "what did you do", "what have you been up to", "what have you been doing lately", "what have you been working on"). Reads Nervice's real action audit log. NOT "what's the plan" (that's orchestrator) and NOT a request to DO something (that's control).
{"route":"news","topic":"<specific subject, or empty for general>"} news = a request for CURRENT EVENTS / world headlines, fetched LIVE and summarized from real results: "what's the news", "what's the news today", "any news", "any tech/cyber/political news" (topic the subject), "news about X" (topic "X"), "headlines", "catch me up on X". A casual check-in or greeting ("what's going on", "what's up", "what's happening", "anything new", "what's the latest update you got") is normal, NOT news — route news ONLY when Nate explicitly asks for world/current-events/headlines. topic = the named subject if there is one, else empty for general headlines. WORLD/current-events ONLY — NOT "what's the news with my project / how's Nervice going" (that's orchestrator), and NOT a reaction to something that just happened (that's normal).
{"route":"normal"} normal = everything else: chat, opinions, simple facts, and any DISCUSSION (vs an explicit action request).

RULES:
- A current-events / headlines question is route news (fetched live) — NEVER answer the news from memory or training.
- A REACTION/FOLLOW-UP about something that just happened ("it didn't open", "that didn't work", "I don't see it") is ALWAYS normal.
- An explicit action request beats discussion; discussion routes normal.
- If the message exactly matches a SAVED TRIGGER below, route skill/run with that trigger.
- Asking ABOUT Nervice's capabilities ("can you play music?", "can you see my screen?", "what can you do?", "are you able to X?") is route normal — the brain answers honestly — NEVER control and NEVER system (telemetry).
- Extract targets/queries minimally and literally; strip polite prefixes ("Jarvis,", "please")."""

_ROUTES = {"hard", "build", "selfmod", "browse", "canvas", "orchestrator", "actions", "news", "control", "skill", "music_mgmt", "system", "normal"}
_CTRL_ACTIONS = {"open_app", "open_url", "play_youtube", "screenshot", "focus_window", "list_windows"}
_SKILL_OPS = {"run", "create", "list", "delete"}
_MUSIC_OPS = {"set", "add", "remove", "list", "clear"}
_SYS_QUESTIONS = {"cpu", "ram", "gpu", "disk", "os", "uptime", "specs", "top_proc", "file_count"}
_ORCH_OPS = {"new", "edit", "critique", "plan", "next", "done", "redo", "status", "gaps", "summary", "result", "workspace"}

# Deterministic guard: a complaint/reaction about a prior action must stay conversational and NEVER
# reach the browse agent or the control interpreter, regardless of what the LLM router decides.
_FOLLOWUP = re.compile(
    r"\b(did(n'?t| not)\s+(appear|open|work|show|launch|come up|do anything|pop up)|"
    r"not\s+(showing|there|appearing|visible|working|here)|"
    r"don'?t\s+see|can'?t\s+see\s+(it|anything|that)|i\s+(want to|wanna)\s+see\s+it|"
    r"where('?s| is| did)\s+it|nothing\s+(happened|appeared|opened|showed)|"
    r"it'?s\s+not\s+(here|showing|there|working|open|up))\b", re.I)

# Deterministic guard: a question about Nervice's OWN abilities ("can you see/read/access/control X",
# "what can you do", "are you able to") asks about CAPABILITIES, not hardware telemetry — it must
# reach the BRAIN (route normal, answered honestly from persona SYSTEM FACTS), NEVER the sysinfo fast
# path. This is the play-some-music / "my computer"-as-path class: a keyword ("screen") used to
# misroute "can you see my screen" into a CPU/RAM/GPU stats dump. Action verbs (open/play/launch) are
# deliberately NOT here, so "can you open notepad" / "can you play music" still route to control.
_CAPABILITY = re.compile(
    r"\bare\s+you\s+able\s+to\b"
    r"|\bwhat\s+can\s+you\s+(?:do|see|access|read|control)\b"
    r"|\bwhat\s+are\s+(?:you\s+able\s+to|your\s+(?:abilities|capabilities|capacities))\b"
    r"|\bdo\s+you\s+have\s+(?:the\s+)?(?:ability|capability|access|power)\b"
    r"|\b(?:can|could|are|do)\s+you\b[^?.!\n]{0,24}\b(?:see|view|watch|look|read|access|control|"
    r"monitor|hear|detect|observe|track|record|capture|tell\s+what|aware\s+of)\b", re.I)


def is_capability_question(msg: str) -> bool:
    """True for a question about Nervice's OWN abilities (vs a request for telemetry values).
    Such questions go to the brain, never the sysinfo telemetry path."""
    return bool(_CAPABILITY.search(msg or ""))


# Display-spec questions sysinfo has NO telemetry for (resolution, refresh rate, brightness, screen
# size). Without this, the LLM maps them to the `specs` enum -> a full CPU/RAM/GPU dump. Route them to
# the brain, which answers honestly ("I read CPU/RAM/GPU/disk/OS, not your display resolution").
_DISPLAY_Q = re.compile(
    r"\bresolution\b|\brefresh\s*rate\b|\bscreen\s+size\b|\bbrightness\b|\baspect\s+ratio\b"
    r"|\bhow\s+(?:big|large|wide)\s+is\s+(?:my|the)\s+(?:screen|display|monitor)\b", re.I)

# web-read pre-guard: a clear "read/summarize a PUBLIC web page" request -> route web_read. Fires on a
# read/summarize verb PLUS a web target (an http(s) URL, OR a page/article/site noun, OR "latest on
# <topic>"). NARROW by design (the doorman-before-comprehension trap): a bare link, the word "read"
# alone, or a read aimed at canvas / the result / the project / notes must NOT match — those stay
# control / canvas / orchestrator. No URL -> the handler web-searches the best public page.
_URL_RE = re.compile(r"https?://[^\s)<>\"']+", re.I)
_READ_VERB = re.compile(
    r"\b(?:summari[sz]e|summary|tl;?dr|the\s+gist|gist\s+of|recap|read|skim)\b"
    r"|\bwhat(?:'?s|\s+does|\s+do)\b[^?.!\n]{0,24}\bsays?\b", re.I)
_WEB_TARGET = re.compile(r"\b(page|article|webpage|web\s*page|site|website|url|link|online|the\s+web|"
                         r"latest\s+on|news\s+(?:on|about)|article\s+about|read\s+about|this\s+(?:link|article|page))\b", re.I)
_NOT_WEBREAD = re.compile(r"\b(canvas|the\s+result|result\s+file|result\.json|my\s+project|the\s+project|"
                          r"the\s+plan|my\s+(?:notes?|files?|email|inbox|skills?|calendar))\b", re.I)


def web_read_route(msg: str) -> "dict | None":
    """Deterministic: a clear read/summarize-a-PUBLIC-page request -> {"route":"web_read","url":...},
    else None. A read verb is REQUIRED; a URL OR a web-target noun supplies the page."""
    m = msg or ""
    if _NOT_WEBREAD.search(m):
        return None                                       # canvas / result / project reads aren't web reads
    if not _READ_VERB.search(m):
        return None
    url = _URL_RE.search(m)
    if url:
        return {"route": "web_read", "url": url.group(0).rstrip(".,);:'\"")}   # read THIS page
    if _WEB_TARGET.search(m):
        return {"route": "web_read", "url": ""}            # no URL -> search + read the best public page
    return None

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
_CANVAS_KW = re.compile(r"\bcanvas\b", re.I)   # both LLMs down -> route to the honest "need the bigger brain"
# Orchestrator (project planner). Keyword net only (both LLM rungs down); read-only ops still work
# fully capped (status/gaps/show plan), LLM ops degrade to an honest "need Groq/Ollama".
_ORCH_KW = re.compile(r"\b(my project|the project|project (?:plan|doc|status)|the plan|plan my project|"
                      r"new project|start (?:a )?(?:new )?project|create (?:a )?project|set ?up (?:a )?project|begin (?:a )?(?:new )?project|"
                      r"change the goal|add a (?:step|requirement)|remove (?:the )?step|reorder|rewrite (?:requirement|step)|change step|"
                      r"next step|next prompt|critique (?:my|the)|show (?:me )?(?:the )?gaps|"
                      r"mark (?:the |this |that )?step|step(?:'?s| is)? done|redo (?:the |this |that )?step)\b", re.I)
# edit-verb patterns: a project EDIT (vs new/critique/plan/etc.) for the keyword-net fallback
_EDIT_KW = re.compile(r"\b(change the goal|change step|add (?:a )?(?:step|requirement)|remove (?:the )?step|reorder|rewrite (?:requirement|step|the))\b", re.I)
# result-loop: Nate pasting Claude Code's report back (keyword net only; the Groq router handles it
# primarily). A leading cue is enough — the rest of the message is the pasted report.
_RESULT_KW = re.compile(r"(here'?s (?:what )?(?:claude ?code|cc)\b|here'?s the (?:result|output|report)\b|"
                        r"\b(?:claude ?code|cc) (?:said|reported|finished)\b|\bpaste (?:the )?result\b|"
                        r"^\s*result\s*:)", re.I)


# "what has Nervice DONE" — its own activity/audit recall (keyword net only).
_ACTIONS_KW = re.compile(r"\b(what (?:have you|did you|you'?ve) (?:done|do|taken|been (?:doing|up to|working on))|"
                         r"actions you'?ve taken|what you'?ve been (?:doing|up to)|your recent activity)\b", re.I)
# current events / world headlines (keyword net only; checked AFTER orchestrator so "news with my
# project" stays orchestrator). General only here — the Groq router extracts a topic when up.
_NEWS_KW = re.compile(r"\b(news|headlines?|what'?s\s+(?:happening|going\s+on)|anything\s+(?:happening|going\s+on)|"
                      r"catch\s+me\s+up|current\s+events|what'?s\s+going\s+on)\b", re.I)

# RECENT-NEWS pre-guard intent: runs BEFORE the public-page web reader so an EXPLICIT current-events
# question routes to the LIVE-headlines `news` path instead of silently returning a Wikipedia article.
# NARROWED (bias-to-conversation): bare phatic check-ins ("what's going on", "what's happening",
# "anything new") and self-referential "latest update YOU received" are NOT news -- they fall to the
# brain (which holds the window). News needs an explicit news/headlines/current-events cue.
_RECENT_NEWS = re.compile(
    r"\b(news|headlines?|breaking(?:\s+news)?|current\s+events?|catch\s+me\s+up|"
    r"(?:happening|going\s+on)\s+in\s+the\s+world|"
    r"latest\s+(?:news|headlines?|on|in|about|developments?)|today'?s\s+(?:news|headlines?|events?)|"
    r"recent\s+(?:news|developments?|events?|headlines?))\b", re.I)
# Self-referential "update(s) YOU/Nervice received/got" or "your latest update" -> about Nervice itself
# (normal/actions), NEVER world news, even when phrased with a news-ish word.
_SELF_UPDATE = re.compile(
    r"\b(?:your|nervice'?s)\s+(?:latest\s+)?(?:news\s+)?updates?\b"
    r"|\bupdates?\b[^?.!\n]{0,20}\byou(?:'?ve)?\s+(?:received|got|gotten|had|have)\b", re.I)
_NEWS_TOPIC = re.compile(r"\b(?:news\s+(?:on|about)|latest\s+(?:on|in|about)|(?:happening|going\s+on)\s+(?:in|with|on))\s+(.+)$", re.I)


def recent_news_route(msg: str) -> "dict | None":
    """Deterministic: a recent-news / today-flavored question with NO specific URL -> {route:news} (the
    LIVE-headlines path), so it never falls into the public-page reader (which returns Wikipedia, not
    live news). A read of a SPECIFIC url/page, or a canvas/project/plan/notes read, is NOT news."""
    m = msg or ""
    if _URL_RE.search(m):                                  # a page was given -> read THAT (web_read), not news
        return None
    if _NOT_WEBREAD.search(m) or _ORCH_KW.search(m):       # canvas/project/plan/notes -> not world news
        return None
    if _SELF_UPDATE.search(m):                             # "latest update YOU received" -> Nervice itself, normal
        return None
    if not _RECENT_NEWS.search(m):
        return None
    tm = _NEWS_TOPIC.search(m)
    topic = re.sub(r"[.?!]+\s*$", "", tm.group(1)).strip().strip("\"'")[:60] if tm else ""
    return {"route": "news", "topic": topic}


def _orch_keyword_op(m: str) -> str | None:
    ml = (m or "").lower()
    if not _ORCH_KW.search(ml):
        return None
    if ("new" in ml or "start" in ml or "create" in ml or "set up" in ml or "begin" in ml) and "project" in ml:
        return "new"
    if _EDIT_KW.search(ml):
        return "edit"
    if "critique" in ml:
        return "critique"
    if "gap" in ml:
        return "gaps"
    if "redo" in ml:
        return "redo"
    if "done" in ml or "finished" in ml:
        return "done"
    if "next" in ml:
        return "next"
    if "how" in ml or "going" in ml or "update" in ml:
        return "summary"
    if "plan" in ml:
        return "plan"
    return "status"


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
    if _CANVAS_KW.search(m):
        return {"route": "canvas"}        # executor enforces smart-brain -> honest "need bigger brain"
    if _ACTIONS_KW.search(m):
        return {"route": "actions"}
    if _RESULT_KW.search(m):
        return {"route": "orchestrator", "op": "result"}
    mo = _orch_keyword_op(m)
    if mo:
        return {"route": "orchestrator", "op": mo}
    if _NEWS_KW.search(m):                              # after orchestrator: "news with my project" stays orchestrator
        return {"route": "news", "topic": ""}          # keyword net = general only (no LLM to extract a topic)
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
    elif route == "orchestrator":
        op = str(out.get("op") or "").strip().lower()
        d["op"] = op if op in _ORCH_OPS else "status"
    elif route == "news":
        d["topic"] = str(out.get("topic") or "").strip()
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
    # An ability/self question ("can you see my screen", "what can you do"), or a display-spec value
    # sysinfo can't read (screen resolution, refresh rate), goes to the BRAIN to be answered honestly —
    # never the sysinfo telemetry fast path (the stats-dump misroute).
    if _CAPABILITY.search(msg) or _DISPLAY_Q.search(msg):
        return {"route": "normal"}
    # A clear read/summarize-a-public-page request -> the FREE owns-nothing web reader (never the
    # metered browse_agent). Deterministic pre-guard: requires real read-a-page intent, not a bare link.
    nr = recent_news_route(msg)                            # recent-news intent -> LIVE headlines, never the
    if nr:                                                 # public-page reader (which returns Wikipedia)
        return nr
    wr = web_read_route(msg)
    if wr:
        return wr
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