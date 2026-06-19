import app.net  # noqa

import re
import sys
import asyncio
import datetime

import httpx
import trafilatura
from ddgs import DDGS

from app.agent import ask_claude, agent_task, browse_agent, claude_turn_ctx
import app.agent as agent_mod
from app import selfmod
from app import weather as weather_mod
from app import sysinfo


async def _fetch_page(client: httpx.AsyncClient, url: str, char_limit: int = 2500) -> str:
    try:
        r = await client.get(url, timeout=8, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
        text = await asyncio.to_thread(trafilatura.extract, r.text)  # CPU-bound; off the loop
        return (text or "")[:char_limit]
    except Exception:
        return ""


async def search_top_url(query: str) -> str | None:
    """The single best PUBLIC-page URL for a query — same DuckDuckGo engine as web_search, but just
    the top http(s) result (no page fetch). None if the search yields nothing usable."""
    try:
        def _ddg():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        results = await asyncio.to_thread(_ddg)
    except Exception:
        return None
    for r in (results or []):
        u = (r.get("href") or r.get("url") or "").strip()
        if u.lower().startswith("http"):
            return u
    return None


async def web_search(query: str, max_results: int = 6) -> str:
    try:
        def _ddg():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        results = await asyncio.to_thread(_ddg)   # DDGS is sync; keep it off the event loop
    except Exception as e:
        return f"Search error: {e}"
    if not results:
        return "No results found (may be rate-limited; try again)."
    # Fetch the first 4 URL-bearing pages CONCURRENTLY (was serial — 8.9s measured) and keep the
    # first 3 that yield text, in result order: same "3 full-text sources" output as before,
    # with one spare to absorb a failed fetch.
    candidates = [u for u in ((r.get("href") or r.get("url") or "") for r in results) if u]
    to_fetch = candidates[:4]
    async with httpx.AsyncClient() as client:
        fetched = await asyncio.gather(*(_fetch_page(client, u) for u in to_fetch))
        bodies = dict(zip(to_fetch, fetched))
        # one bounded retry wave if most of the first wave came back empty — keeps the old
        # "keep trying further results" coverage without its serial worst case
        if sum(1 for b in bodies.values() if b) < 2 and candidates[4:6]:
            more = candidates[4:6]
            fetched2 = await asyncio.gather(*(_fetch_page(client, u) for u in more))
            bodies.update(dict(zip(more, fetched2)))
    blocks = []
    kept = 0
    for r in results:
        url = r.get("href") or r.get("url") or ""
        title = r.get("title", "")
        snippet = r.get("body", "")
        body = bodies.get(url, "") if kept < 3 else ""
        if body:
            kept += 1
        blocks.append(f"SOURCE: {title}\nURL: {url}\nSNIPPET: {snippet}" + (f"\nFULL TEXT:\n{body}" if body else ""))
    return "\n\n---\n\n".join(blocks)


async def consult_claude(task: str) -> str:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[CLAUDE CALL {ts}] {task[:120]}", file=sys.stderr)
    try:
        with open("logs/claude_calls.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{task[:300]}\n")
    except FileNotFoundError:
        import os; os.makedirs("logs", exist_ok=True)
        with open("logs/claude_calls.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{task[:300]}\n")
    # Thread the turn's real system (persona + SYSTEM FACTS + safety floor + memories, + voice
    # addendum) and conversation window so the hard route answers WITH context, not from the bare
    # synthesized question. Falls back to the plain task if no turn context is set (e.g. direct call).
    ctx = claude_turn_ctx.get()
    if ctx and ctx.get("messages"):
        return await ask_claude(task, system=ctx.get("system"), messages=ctx.get("messages"))
    return await ask_claude(task)


async def get_weather() -> str:
    """Local weather for Nate's area (coords hardcoded in app/weather.py — no location needed)."""
    w = await weather_mod.get_weather()
    return w or "weather service unavailable right now"


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Nate's local weather RIGHT NOW: current temperature, conditions, and today's "
                       "high/low. His location is already configured — NEVER ask which city or region. "
                       "Always use this for any weather, temperature, or forecast question; never use "
                       "web_search for weather.",
        "parameters": {"type": "object", "properties": {}},
    },
}

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current or factual info you may not know — recent events, current data, anything that may have changed since training. Returns top results as text.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "search query"}},
            "required": ["query"],
        },
    },
}

async def agent_build(task: str) -> str:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[BUILD CALL {ts}] {task[:120]}", file=sys.stderr)
    result = await agent_task(task)
    cost = (getattr(agent_mod, "last_run", None) or {}).get("cost_usd")
    try:
        import os; os.makedirs("logs", exist_ok=True)
        with open("logs/claude_calls.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\tBUILD\tcost={cost}\t{task[:300]}\n")
    except Exception:
        pass
    return result


# Bare "open <site>" with nothing to find/read/report: launching the headless server browser is
# the wrong tool — Nate never sees its tabs, he just waits. Guard fires only when the task starts
# with an open-verb AND names no actual task; everything else still goes to the real agent.
_BARE_OPEN_RE = re.compile(
    r"^\s*(?:please\s+)?(?:open|go\s+to|visit|pull\s+up|launch|bring\s+up)\b", re.I)
_BROWSE_TASK_WORDS = re.compile(
    r"\b(tell|find|check|read|what|which|who|how|search|look|report|top|latest|news|summar\w*|"
    r"extract|list|count|price|headline|story|video|trending|review|compare|describe|click|"
    r"buy|order|add)\b", re.I)   # 'play'/'watch' REMOVED — music playback never escalates to the browser agent

# Music/video PLAYBACK must NEVER drive the visible browser agent (slow, $, wall-of-text transcript).
# It goes to the fast, deterministic play_youtube path instead — the root cause of the 89s/$0.26 lofi run.
_MUSIC_PLAY_TASK = re.compile(
    r"\b(?:play|listen\s+to|put\s+on)\b[^.]{0,40}\b(?:music|song|songs|playlist|mix|track|tracks|tune|"
    r"tunes|album|artist|lofi|lo-fi|radio|beats|hip\s*hop|jazz|rock|pop)\b"
    r"|\b(?:youtube|spotify)\b[^.]{0,40}\bplay\b"
    r"|\bplay\b[^.]{0,40}\bon\s+(?:youtube|spotify)\b", re.I)


def _play_query_from_browse_task(task: str) -> str:
    """Strip browse boilerplate ('search YouTube for …', '… and play the first result') so the
    play_youtube parser receives the genre/vibe words, not the agent instruction."""
    s = task or ""
    s = re.sub(r"\b(?:search|find|look\s+up|go\s+to|open|on)\s+(?:for\s+)?(?:youtube|yt|spotify)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:and\s+)?(?:then\s+)?play\s+(?:the\s+)?(?:first|top|1st)\s+(?:result|video|one|hit|song)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:search|find|look\s+up)\s+for\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s or (task or "")


async def browse(task: str) -> str:
    # MUSIC/VIDEO PLAYBACK -> the fast deterministic play_youtube path; NEVER the browser agent.
    if _MUSIC_PLAY_TASK.search(task or ""):
        from app import computer
        q = _play_query_from_browse_task(task)
        print(f"[BROWSE -> play_youtube redirect] {task[:120]}", file=sys.stderr)
        return computer.play_youtube(q)
    if _BARE_OPEN_RE.match(task) and not _BROWSE_TASK_WORDS.search(task):
        print(f"[BROWSE skipped — bare open] {task[:120]}", file=sys.stderr)
        return ("Just opening that site wouldn't show you anything — my browser runs on the "
                "server, not your screen. Tell me what you want from it and I'll go read it "
                "and report back.")
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[BROWSE {ts}] {task[:120]}", file=sys.stderr)
    result = await browse_agent(task)
    cost = (getattr(agent_mod, "last_run", None) or {}).get("cost_usd")
    try:
        import os; os.makedirs("logs", exist_ok=True)
        with open("logs/claude_calls.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\tBROWSE\tcost={cost}\t{task[:300]}\n")
    except Exception:
        pass
    return result


async def propose_self_update(instruction: str) -> str:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(f"[SELFMOD {ts}] {instruction[:120]}", file=sys.stderr)
    rec = await selfmod.propose(instruction)
    cost = (getattr(agent_mod, "last_run", None) or {}).get("cost_usd")
    try:
        import os; os.makedirs("logs", exist_ok=True)
        with open("logs/claude_calls.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\tSELFMOD\tcost={cost}\t{rec.get('status')}\t{instruction[:300]}\n")
    except Exception:
        pass
    if rec.get("status") == "pending":
        return (f"Proposal {rec['id']} created: {rec.get('summary', '')} "
                f"Review with 'show {rec['id']}', then 'approve {rec['id']}' or 'reject {rec['id']}'.")
    return f"Proposal not created — {rec.get('reason', 'rejected')}. (id {rec['id']})"


CONSULT_CLAUDE_TOOL = {
    "type": "function",
    "function": {
        "name": "consult_claude",
        "description": "Consult Claude, a far more capable (and metered) expert model. CALL IT when the task matches ANY of: (1) the user explicitly asks for Claude; (2) formal logic puzzles, riddles, or brainteasers with multiple interacting constraints; (3) mathematical proofs or multi-step quantitative problems beyond basic algebra; (4) designing or reviewing nontrivial code architecture or database schemas; (5) long rigorous analysis where a wrong answer is costly. DO NOT call it for casual chat, simple factual questions, news or current events (use web_search), or everyday tasks. When in doubt on category 2 or 3, CALL IT — a famous-sounding puzzle is usually harder than it looks. Pass a self-contained task with all needed context — Claude has no memory of this conversation.",
        "parameters": {"type": "object",
            "properties": {"task": {"type": "string", "description": "complete, self-contained task for Claude including all relevant context"}},
            "required": ["task"]},
    },
}

AGENT_BUILD_TOOL = {
    "type": "function",
    "function": {
        "name": "agent_build",
        "description": "Delegate file creation/editing to the builder agent. It works in a jailed workspace folder and can create complete multi-file projects (websites, scripts, configs). Use when the user asks to build, create, edit, or fix actual FILES. Not for explaining code in chat. Pass a complete, self-contained build spec.",
        "parameters": {"type": "object",
            "properties": {"task": {"type": "string", "description": "complete build spec with all requirements"}},
            "required": ["task"]},
    },
}

PROPOSE_SELF_UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_self_update",
        "description": "Create a proposal to change Nervice's own behavior/code (persona, routing, tools). Use when the user asks Nervice to change how IT behaves or works. The proposal requires the user's explicit approval before anything is applied.",
        "parameters": {"type": "object",
            "properties": {"instruction": {"type": "string", "description": "what to change about Nervice's own behavior or code, in plain language"}},
            "required": ["instruction"]},
    },
}

BROWSE_TOOL = {
    "type": "function",
    "function": {
        "name": "browse",
        "description": "Drive a real visible browser: open sites, read pages, click through, report back. Use when the user asks to open/check/browse a specific website or do something ON a site. Not for general factual lookups (web_search is cheaper/faster for those).",
        "parameters": {"type": "object",
            "properties": {"task": {"type": "string", "description": "what to do in the browser, including the target site/URL"}},
            "required": ["task"]},
    },
}

# ---------------------------- read-only system awareness (app/sysinfo.py) ----------------------------
# All accept **_ : the tool model sometimes emits a stray empty-key argument for no-arg tools, which
# would otherwise raise "unexpected keyword argument ''" — absorb and ignore it.
async def get_system_info(**_) -> str:
    return await asyncio.to_thread(sysinfo.get_system_info)


async def get_top_processes(by: str = "memory", n: int = 5, **_) -> str:
    return await asyncio.to_thread(sysinfo.get_top_processes, by, n)


async def count_files(path: str | None = None, **_) -> str:
    return await asyncio.to_thread(sysinfo.count_files, path)


async def get_news(topic: str = "", **_) -> str:
    """Real current headlines via a LIVE web search at ask time. `topic` searches that subject
    ("the election", "cybersecurity"); empty pulls Nate's standing topics. The grounded-synthesis
    step then summarizes ONLY from these results — no fabricated headlines, never from training."""
    t = (topic or "").strip()
    q = f"{t} news latest" if t else "latest news headlines today politics technology cybersecurity"
    return await web_search(q, max_results=8)


SYSTEM_INFO_TOOL = {
    "type": "function",
    "function": {
        "name": "get_system_info",
        "description": "THIS computer's hardware and live stats: CPU model + cores + current usage %, "
                       "RAM used/total, GPU model + VRAM + temp, disk space, uptime, OS. Use for ANY "
                       "question about the machine's specs or current state — 'what CPU/GPU do I have', "
                       "'how much RAM/disk', 'how busy is it', 'what OS'. Read-only.",
        "parameters": {"type": "object", "properties": {}},
    },
}

TOP_PROCESSES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_top_processes",
        "description": "Real running processes sorted by memory or CPU use. Use for 'what's using the "
                       "most memory/CPU', 'what's eating my RAM', 'what's running'. Read-only — it only "
                       "reads process stats, it never closes or kills anything.",
        "parameters": {"type": "object", "properties": {
            "by": {"type": "string", "enum": ["memory", "cpu"], "description": "sort key (default memory)"},
            "n": {"type": "integer", "description": "how many to list (default 5)"}}},
    },
}

COUNT_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "count_files",
        "description": "Count files and folders under a path. Use for 'how many files in my Downloads', "
                       "'how many files do I have'. Pass a folder name (e.g. 'Downloads') or full path; "
                       "omit for the home folder. Read-only — counts names only, never reads file "
                       "contents; capped in time/size so it can't hang.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "folder name or path; omit for home"}}},
    },
}

GET_NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "Current news / headlines / what's happening, fetched LIVE. ALWAYS use this for "
                       "ANY news request ('the news', 'what's happening', 'catch me up') — it returns "
                       "REAL web results. Pass `topic` for a specific subject ('the election', "
                       "'cybersecurity'); omit for Nate's general topics. NEVER answer a news question "
                       "from your own knowledge.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "a specific subject to search; omit for general headlines"}}},
    },
}

TOOLS = [WEATHER_TOOL, WEB_SEARCH_TOOL, CONSULT_CLAUDE_TOOL, AGENT_BUILD_TOOL,
         PROPOSE_SELF_UPDATE_TOOL, BROWSE_TOOL,
         SYSTEM_INFO_TOOL, TOP_PROCESSES_TOOL, COUNT_FILES_TOOL, GET_NEWS_TOOL]
TOOL_FUNCS = {"get_weather": get_weather, "web_search": web_search, "consult_claude": consult_claude,
              "agent_build": agent_build, "propose_self_update": propose_self_update, "browse": browse,
              "get_system_info": get_system_info, "get_top_processes": get_top_processes,
              "count_files": count_files, "get_news": get_news}
