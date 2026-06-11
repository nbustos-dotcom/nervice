"""Phase-1 tests: the Ollama qwen3.5:4b rung. t1 client (real Ollama). Later sections land with
their commits: t2 429->ollama, t3 ollama-down->claude, t4 tools on the rung, t5 hard->claude,
t6 context, t7 budget-path untouched. Throwaway."""
import sys, pathlib, asyncio, time, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import httpx
from groq import RateLimitError
import app.llm as llm
import app.router as router
import app.ollama_client as oc

R = {}


def groq_429():
    req = httpx.Request("POST", "https://api.groq.com/v1")
    return RateLimitError("Rate limit reached (TPD)", response=httpx.Response(429, request=req), body=None)


async def main():
    # ============ t1: hardened client against REAL Ollama ============
    up = await oc.is_up()
    R["t1a is_up"] = up is True
    print(f"t1a: is_up={up}")

    t0 = time.time()
    m = await oc.chat([{"role": "user", "content": "one word: what color is grass?"}])
    dt = time.time() - t0
    R["t1b chat answers"] = "green" in (m.get("content") or "").lower()
    print(f"t1b: {dt*1000:.0f}ms content={m.get('content','')[:40]!r}")

    TOOLS = [{"type": "function", "function": {
        "name": "get_weather", "description": "Nate's local weather right now. Use for any weather question.",
        "parameters": {"type": "object", "properties": {}}}}]
    m2 = await oc.chat([{"role": "user", "content": "what's the weather like?"}], tools=TOOLS)
    calls = m2.get("tool_calls") or []
    R["t1c tool call fires"] = bool(calls) and calls[0]["function"]["name"] == "get_weather"
    print(f"t1c: tool_calls={[c['function']['name'] for c in calls]}")

    # think:false + keep_alive baked in: a 3-sentence answer must be fast (no 20s thinking stall)
    t0 = time.time()
    m3 = await oc.chat([{"role": "user", "content": "in 2 short sentences, why is the sky blue?"}])
    dt3 = time.time() - t0
    R["t1d no thinking stall"] = dt3 < 8.0 and len((m3.get("content") or "").split()) > 6
    print(f"t1d: {dt3*1000:.0f}ms (must be <8s — thinking off)")

    # json classification shape (used by the router rung)
    try:
        j = await oc.chat_json('Output ONLY JSON like {"route":"control"} or {"route":"normal"}. '
                               'control = a request to open/launch an app or take a screenshot on the PC. '
                               'normal = anything else.', "open notepad")
        R["t1e chat_json"] = j.get("route") == "control"
        print(f"t1e: {j}")
    except Exception as e:
        R["t1e chat_json"] = False
        print(f"t1e: ERR {repr(e)[:80]}")

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
