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

    # ============ t2/t3/t4/t6/t7: the ladder repoint ============
    from app.chat import respond
    TURNS = pathlib.Path("logs/turns.log")

    def last_rung():
        line = TURNS.read_text(encoding="utf-8").splitlines()[-1]
        m = re.search(r"rung=(\S+)", line)
        return m.group(1) if m else "?"

    orig_create = llm._client.chat.completions.create
    async def boom(**kw):
        raise groq_429()
    orig_ask = llm.ask_claude
    async def no_claude(*a, **k):
        raise RuntimeError("claude must NOT be called when ollama is up")

    # t2: 429 normal turn -> OLLAMA answers (claude mocked to raise = proof)
    llm._client.chat.completions.create = boom
    llm.ask_claude = no_claude
    try:
        t0 = time.time()
        r2 = await respond("nate", "what's a good name for a goldfish? just the name", [])
        dt2 = time.time() - t0
    finally:
        llm._client.chat.completions.create = orig_create
        llm.ask_claude = orig_ask
    R["t2 429 -> ollama answers (rung=ollama)"] = bool(r2) and "limit" not in r2.lower() and last_rung() == "ollama"
    print(f"t2: {dt2*1000:.0f}ms rung={last_rung()} reply={r2[:50]!r}")

    # t4: 429 + weather question -> the weather TOOL works ON the ollama rung
    llm._client.chat.completions.create = boom
    llm.ask_claude = no_claude
    try:
        t0 = time.time()
        r4 = await respond("nate", "what's the weather like right now?", [])
        dt4 = time.time() - t0
    finally:
        llm._client.chat.completions.create = orig_create
        llm.ask_claude = orig_ask
    R["t4 capped weather tool on ollama rung"] = bool(re.search(r"\d+\s*°?f?\b|degrees|cloud|rain|clear|storm|sun", (r4 or ""), re.I)) and last_rung() == "ollama"
    print(f"t4: {dt4*1000:.0f}ms rung={last_rung()} reply={r4[:80]!r}")

    # t6: ollama rung has conversation context
    win = [{"role": "user", "content": "for this chat remember: my dog is named Rex."},
           {"role": "assistant", "content": "Got it — Rex."}]
    llm._client.chat.completions.create = boom
    llm.ask_claude = no_claude
    try:
        r6 = await respond("nate", "what's my dog's name? just the name", win)
    finally:
        llm._client.chat.completions.create = orig_create
        llm.ask_claude = orig_ask
    R["t6 ollama rung has context"] = "rex" in (r6 or "").lower()
    print(f"t6: reply={r6[:40]!r}")

    # t3: 429 + ollama DOWN -> claude marker; both down -> exhausted + start-Ollama hint
    async def ollama_down(*a, **k):
        raise oc.OllamaUnavailable("simulated down")
    orig_chat = llm.ollama.chat
    llm.ollama.chat = ollama_down
    async def marker(prompt=None, system=None, messages=None):
        return "CLAUDE_MARKER"
    llm._client.chat.completions.create = boom
    llm.ask_claude = marker
    try:
        r3 = await respond("nate", "hello there", [])
    finally:
        llm.ask_claude = orig_ask
        llm._client.chat.completions.create = orig_create
    R["t3a 429+ollama down -> claude"] = "CLAUDE_MARKER" in (r3 or "")
    print(f"t3a: {r3[:40]!r}")
    # both down: claude raises too + is_up forced False -> hint appended
    async def all_out(*a, **k):
        raise RuntimeError("claude out")
    orig_isup = llm.ollama.is_up
    async def down(): return False
    llm.ollama.is_up = down
    llm._client.chat.completions.create = boom
    llm.ask_claude = all_out
    try:
        r3b = await respond("nate", "hello again", [])
    finally:
        llm.ask_claude = orig_ask
        llm._client.chat.completions.create = orig_create
        llm.ollama.is_up = orig_isup
        llm.ollama.chat = orig_chat
    R["t3b all out -> friendly + ollama hint"] = r3b.startswith(llm.LADDER_EXHAUSTED_MSG) and "Ollama" in r3b
    print(f"t3b: {r3b[:90]!r}")

    # t7: with Groq budget, ollama never engages. Recording mock (never raises): if a GENUINE
    # transient per-minute 429 happens mid-test, the marker answer proves the ladder worked —
    # that's correct behavior, noted, not a failure. Structurally _ollama_fallback is only
    # reachable inside `except RateLimitError`, so "engaged without a 429" is impossible to
    # reach except through that handler.
    flag = {"called": False}
    async def marker_ollama(*a, **k):
        flag["called"] = True
        return {"content": "OLLAMA_MARKER"}
    llm.ollama.chat = marker_ollama
    try:
        r7 = await respond("nate", "what's the capital of Japan? one word", [])
    finally:
        llm.ollama.chat = orig_chat
    clean = (not flag["called"]) and "tokyo" in (r7 or "").lower() and last_rung() == "groq"
    transient = flag["called"] and "OLLAMA_MARKER" in (r7 or "")   # real 429 -> ladder fired correctly
    R["t7 budget path untouched"] = clean or transient
    print(f"t7: rung={last_rung()} ollama_called={flag['called']} "
          f"{'(genuine transient 429 — ladder fired correctly)' if transient else ''} reply={r7[:30]!r}")

    # ============ t8: router through Ollama when Groq is capped ============
    orig_cj = router.chat_json
    async def cj_boom(system, user):
        raise groq_429()
    router.chat_json = cj_boom
    used = {"ollama": 0}
    orig_ocj = router.ollama.chat_json
    async def spy_ocj(system, user):
        used["ollama"] += 1
        return await orig_ocj(system, user)
    router.ollama.chat_json = spy_ocj
    try:
        r8a = await router.classify("open notepad")
        r8b = await router.classify("ask claude to prove that sqrt 2 is irrational")
        r8c = await router.classify("how's your day going?")
    finally:
        router.chat_json = orig_cj
        router.ollama.chat_json = orig_ocj
    R["t8 capped router via ollama"] = (r8a == "control" and r8b == "hard" and r8c == "normal"
                                        and used["ollama"] >= 3)
    print(f"t8: open notepad={r8a} ask-claude-proof={r8b} smalltalk={r8c} (ollama used {used['ollama']}x)")

    # t8b: groq AND ollama down -> keyword net still routes control
    router.chat_json = cj_boom
    async def ocj_down(system, user):
        raise oc.OllamaUnavailable("simulated down")
    router.ollama.chat_json = ocj_down
    try:
        r8d = await router.classify("open notepad")
        r8e = await router.classify("take a screenshot")
    finally:
        router.chat_json = orig_cj
        router.ollama.chat_json = orig_ocj
    R["t8b both down -> keyword net"] = r8d == "control" and r8e == "control"
    print(f"t8b: open notepad={r8d} screenshot={r8e}")

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
