import re

from app.llm import chat_json

ROUTER_SYSTEM = """Classify the user's message into exactly one route. Output ONLY JSON: {"route":"hard"} or {"route":"build"} or {"route":"selfmod"} or {"route":"browse"} or {"route":"control"} or {"route":"normal"}.
The key test for selfmod/build/browse is whether the user is making an EXPLICIT ACTION REQUEST, not merely discussing, asking an opinion, or asking how something could be done. Discussion routes to normal.

selfmod = an explicit request to change Nervice's OWN behavior, personality, rules, or code ("stop ending sentences with questions", "change your persona to be warmer", "update yourself to do X"). Asking Nervice's opinion of itself, discussing self-improvement, or asking what could be improved is NOT selfmod — it's normal.
  selfmod: "stop being so wordy from now on"   vs   normal: "what do you think could be improved about yourself?"
build = an explicit request to create, edit, or fix actual FILES/projects (a script, a webpage, a config). Explaining, designing, or discussing code in chat is NOT build.
  build: "build me a landing page in the workspace"   vs   normal: "how would you structure a landing page?"
browse = an explicit request to find, check, read, or report something ON a specific site — there is a TASK with an answer to bring back. General factual questions or current events are NOT browse.
  browse: "open hacker news and tell me the top story"   vs   normal: "what's the latest AI news?"
control = a request for Nervice to DO (act/change) something on Nate's OWN computer: open or launch an app ("open notepad", "launch spotify", "open calculator"), open a website in his browser ("open youtube", "pull up reddit", "go to amazon"), take a screenshot, list or switch windows, OR any file/app/system ACTION on the machine (close an app, delete/move files, install, change settings). If the message names something to FIND, READ, CHECK, or REPORT on a site, it is browse, not control.
  control: "open youtube" / "open notepad" / "take a screenshot" / "close chrome" / "delete my downloads"   vs   browse: "open youtube and tell me the top trending video"
  NOT control — these are READ-ONLY QUESTIONS about the machine, route them "normal" (the system-info tools answer them): "what CPU/GPU do I have", "how much RAM/disk space", "how many files in my Downloads", "what's using the most memory", "what OS am I on", "how busy is the CPU". control is only an ACTION; a question about specs/stats/processes/file counts is normal.
hard = formal logic puzzles/riddles/brainteasers with interacting constraints; mathematical proofs or multi-step quantitative problems beyond basic algebra; design or review of nontrivial code architecture or database schemas; long rigorous analysis where wrong answers are costly; or the user explicitly asks for Claude.
normal = everything else: chat, opinions (including opinions ABOUT Nervice itself), simple facts, news/current events, everyday tasks, and any DISCUSSION (as opposed to an explicit action request) of building, browsing, or self-change.
A REACTION or FOLLOW-UP about something that just happened is ALWAYS normal — never browse or control: "it didn't appear", "I don't see it", "I want to see it", "that didn't work", "where is it", "nothing happened", "it's not showing". These are conversation about a previous action, not a new request. browse requires the user to NAME a specific website to read; if no site is named, it is not browse.
Classify the TASK TYPE — an explicit action request vs. a discussion. Ignore whether the question seems easy or famous."""

# Deterministic guard: a complaint/reaction about a prior action must stay conversational and NEVER
# reach the browse agent or the control interpreter, regardless of what the LLM router decides.
_FOLLOWUP = re.compile(
    r"\b(did(n'?t| not)\s+(appear|open|work|show|launch|come up|do anything|pop up)|"
    r"not\s+(showing|there|appearing|visible|working|here)|"
    r"don'?t\s+see|can'?t\s+see\s+(it|anything|that)|i\s+(want to|wanna)\s+see\s+it|"
    r"where('?s| is| did)\s+it|nothing\s+(happened|appeared|opened|showed)|"
    r"it'?s\s+not\s+(here|showing|there|working|open|up))\b", re.I)


# Deterministic guard: a READ-ONLY question about the machine's specs/stats/processes/files is a
# normal turn (the system-info tools answer it), NEVER the control gate's "I can only open apps".
# Forcing "normal" is safe (the tool model still picks the right tool); we skip it when an action
# verb is present so real control actions (open/close/delete...) still route to control.
_SYSINFO_NOUN = re.compile(r"\b(cpu|gpu|graphics card|processor|cores?|ram|memory|disk|storage|drive|"
                           r"space|specs?|hardware|uptime|operating system|\bos\b|processes?|files?|folders?)\b", re.I)
_SYSINFO_ASK = re.compile(r"\b(what|which|how\s+(?:much|many|big|busy)|do\s+i\s+have|how'?s|"
                          r"tell me|show me|using the most|what'?s\s+(?:using|running|eating))\b", re.I)
_MACHINE_ACTION = re.compile(r"\b(open|launch|start|close|quit|kill|terminate|end|stop|delete|remove|"
                             r"erase|wipe|empty|clear|format|trash|move|rename|install|uninstall|"
                             r"screenshot|switch|focus|run|create|make|shut\s*down|shutdown|reboot|restart)\b", re.I)


async def classify(user_message: str) -> str:
    msg = user_message or ""
    # A reaction/complaint about a prior action is conversation — keep it normal so it can't fall
    # through to the browse agent's "my browser runs on the server" boilerplate or the control gate.
    if _FOLLOWUP.search(msg):
        return "normal"
    # A read-only machine question -> normal (system-info tools), not control. Action verbs excluded.
    if _SYSINFO_ASK.search(msg) and _SYSINFO_NOUN.search(msg) and not _MACHINE_ACTION.search(msg):
        return "normal"
    try:
        out = await chat_json(ROUTER_SYSTEM, user_message)
        route = out.get("route")
        return route if route in ("hard", "build", "selfmod", "browse", "control") else "normal"
    except Exception:
        return "normal"   # fail open to the free path
