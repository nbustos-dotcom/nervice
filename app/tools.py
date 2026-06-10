import app.net  # noqa

import sys
import datetime

import httpx
import trafilatura
from ddgs import DDGS

from app.agent import ask_claude, agent_task
import app.agent as agent_mod
from app import selfmod


def _fetch_page(url: str, char_limit: int = 2500) -> str:
    try:
        r = httpx.get(url, timeout=8, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return ""
        text = trafilatura.extract(r.text) or ""
        return text[:char_limit]
    except Exception:
        return ""


def web_search(query: str, max_results: int = 6) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"Search error: {e}"
    if not results:
        return "No results found (may be rate-limited; try again)."
    blocks = []
    fetched = 0
    for r in results:
        url = r.get("href") or r.get("url") or ""
        title = r.get("title", "")
        snippet = r.get("body", "")
        body = ""
        if fetched < 3 and url:
            body = _fetch_page(url)
            if body:
                fetched += 1
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

TOOLS = [WEB_SEARCH_TOOL, CONSULT_CLAUDE_TOOL, AGENT_BUILD_TOOL, PROPOSE_SELF_UPDATE_TOOL]
TOOL_FUNCS = {"web_search": web_search, "consult_claude": consult_claude,
              "agent_build": agent_build, "propose_self_update": propose_self_update}
