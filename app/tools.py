import app.net  # noqa

import httpx
import trafilatura
from ddgs import DDGS


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

TOOLS = [WEB_SEARCH_TOOL]
TOOL_FUNCS = {"web_search": web_search}
