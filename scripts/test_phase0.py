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

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
