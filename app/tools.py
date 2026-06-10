import app.net  # noqa

import re
import sys
import asyncio
import datetime

import httpx
import trafilatura
from ddgs import DDGS

from app.agent import ask_claude, agent_task, browse_agent
import app.agent as agent_mod
from app import selfmod
from app import weather as weather_mod


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
    r"play|watch|buy|order|add)\b", re.I)


async def browse(task: str) -> str:
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

TOOLS = [WEATHER_TOOL, WEB_SEARCH_TOOL, CONSULT_CLAUDE_TOOL, AGENT_BUILD_TOOL,
         PROPOSE_SELF_UPDATE_TOOL, BROWSE_TOOL]
TOOL_FUNCS = {"get_weather": get_weather, "web_search": web_search, "consult_claude": consult_claude,
              "agent_build": agent_build, "propose_self_update": propose_self_update, "browse": browse}
