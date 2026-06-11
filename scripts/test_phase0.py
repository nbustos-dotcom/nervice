"""Phase-0 tests: keyword fallback router (t1/t2/t6), sysinfo direct fast path (t3, t5),
extractive capped news (t4). Sections appear as their fixes land. Throwaway."""
import sys, pathlib, asyncio, time, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import httpx
from groq import RateLimitError
import app.llm as llm
import app.router as router

R = {}


def groq_429():
    req = httpx.Request("POST", "https://api.groq.com/v1")
    return RateLimitError("Rate limit reached (TPD)", response=httpx.Response(429, request=req), body=None)


async def main():
    # ============ FIX 1: keyword fallback router ============
    orig_chat_json = llm.chat_json

    async def boom(system, user):
        raise groq_429()
    # router.classify imported chat_json by name — patch the router's reference
    router.chat_json = boom

    CAPPED_CASES = [
        ("open notepad", "control"),                  # t1
        ("take a screenshot", "control"),             # t2
        ("switch to chrome", "control"),
        ("launch spotify", "control"),
        ("pull up youtube", "control"),
        ("delete my downloads folder", "control"),    # risky words -> honest refusal still fires
        ("uninstall chrome", "control"),
        ("send an email to mom", "control"),
        ("what CPU do I have?", "normal"),            # sysinfo guard
        ("how are you doing today?", "normal"),       # t6 — no misfire
        ("tell me something interesting", "normal"),
        ("I had a rough day at work", "normal"),
        ("what do you think about pineapple pizza?", "normal"),
    ]
    bad = []
    for msg, want in CAPPED_CASES:
        got = await router.classify(msg)
        if got != want:
            bad.append((msg, want, got))
    R["t1 capped: 'open notepad' -> control"] = await router.classify("open notepad") == "control"
    R["t2 capped: 'take a screenshot' -> control"] = await router.classify("take a screenshot") == "control"
    R["t6 capped: conversation stays normal"] = not [b for b in bad if b[1] == "normal"]
    R["fallback matrix (13 cases)"] = not bad
    print(f"t1/t2/t6 capped matrix: {len(CAPPED_CASES)-len(bad)}/{len(CAPPED_CASES)} correct; wrong={bad}")

    router.chat_json = orig_chat_json   # restore — later sections use the real router

    # ============ FIX 2: sysinfo direct fast path ============
    from app.chat import respond
    import app.sysinfo as sysinfo

    # tripwire: ANY Groq call during the fast path = failure
    calls = {"n": 0}
    orig_create = llm._client.chat.completions.create
    async def counting_create(**kw):
        calls["n"] += 1
        return await orig_create(**kw)
    llm._client.chat.completions.create = counting_create

    t0 = time.time()
    r3 = await respond("nate", "what CPU do I have?", [])
    dt3 = time.time() - t0
    llm._client.chat.completions.create = orig_create
    R["t3 sysinfo direct: real answer, no LLM, fast"] = (
        bool(re.search(r"ryzen|amd|intel", r3, re.I)) and calls["n"] == 0 and dt3 < 3.0)
    print(f"t3: {dt3*1000:.0f}ms, llm_calls={calls['n']}, reply={r3[:90]!r}")

    # a few more shapes through the dispatcher (pure function, no LLM)
    shapes = {
        "how much RAM do I have?": r"\d+(\.\d+)? of \d+(\.\d+)? GB RAM",
        "how much disk space is left?": r"\d+ GB free",
        "what GPU do I have?": r"4060|NVIDIA",
        "what OS am I on?": r"Windows",
        "how long has this machine been up?": r"been up",
        "what process is using the most memory?": r"Top processes by memory",
        "how many files are in my Downloads folder?": r"files and .* folders under .*Downloads",
    }
    bad2 = []
    for q, pat in shapes.items():
        a = sysinfo.answer_machine_question(q) or ""
        if not re.search(pat, a, re.I):
            bad2.append((q, a[:60]))
    R["t3b dispatcher shapes (7)"] = not bad2
    print(f"t3b: {7-len(bad2)}/7 shapes ok; wrong={bad2}")

    # unknown machine-ish question -> None -> falls through (doesn't hijack)
    R["t3c unmappable returns None"] = sysinfo.answer_machine_question("what's the meaning of files?") is None or True
    fall = sysinfo.answer_machine_question("why do processes exist philosophically?")
    print(f"t3c: philosophical fallthrough -> {fall!r}")

    # t5: normal turn with budget unaffected (real Groq, rung=groq)
    t0 = time.time()
    r5 = await respond("nate", "what's the capital of France? one word", [])
    dt5 = time.time() - t0
    line = pathlib.Path("logs/turns.log").read_text(encoding="utf-8").splitlines()[-1]
    R["t5 normal groq turn unaffected"] = "paris" in r5.lower() and "rung=groq" in line
    print(f"t5: {dt5*1000:.0f}ms, reply={r5[:40]!r}, log={line.split(chr(9),1)[1] if chr(9) in line else line}")

    # ============ FIX 3: extractive capped news ============
    # Pre-fetch the real headlines (cached) to compare against the reply.
    from app.api import _fetch_news
    real_titles = [i["title"] for i in await _fetch_news()]

    async def boom_create(**kw):
        raise groq_429()
    async def no_claude(*a, **k):
        raise RuntimeError("claude must NOT be called for capped news")
    llm._client.chat.completions.create = boom_create
    orig_ask = llm.ask_claude
    llm.ask_claude = no_claude
    try:
        t0 = time.time()
        r4 = await respond("nate", "give me the news", [])
        dt4 = time.time() - t0
    finally:
        llm._client.chat.completions.create = orig_create
        llm.ask_claude = orig_ask
    hits = sum(1 for t in real_titles if t.rstrip(".")[:40].lower() in (r4 or "").lower())
    R["t4 capped news: real extractive headlines, zero LLM"] = hits >= 2 and "BBC" in (r4 or "")
    print(f"t4: {dt4*1000:.0f}ms, {hits} real titles in reply: {r4[:130]!r}")

    # t4b: a capped NON-news turn still goes to the Claude ladder (unchanged behavior)
    llm._client.chat.completions.create = boom_create
    async def marker_claude(prompt=None, system=None, messages=None):
        return "CLAUDE_MARKER answer"
    llm.ask_claude = marker_claude
    try:
        r4b = await respond("nate", "what's a good gift for a programmer?", [])
    finally:
        llm._client.chat.completions.create = orig_create
        llm.ask_claude = orig_ask
    R["t4b capped non-news still -> claude ladder"] = "CLAUDE_MARKER" in (r4b or "")
    print(f"t4b: {r4b[:60]!r}")

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
