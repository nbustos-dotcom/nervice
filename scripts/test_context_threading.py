"""Context-threading tests: every Claude rung must receive the conversation window, not a bare
message (the mid-conversation amnesia bug). A 3-turn window establishes 'my dog is named Rex';
turn 3 asks the dog's name through each rung -> the reply must say 'Rex'. Real Groq + Claude
(Pro). Cleans up its DB rows. Throwaway."""
import sys, pathlib, asyncio, uuid, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import httpx
from groq import RateLimitError
import app.llm as llm
import app.tools as tools
from app.llm import chat_with_tools, LADDER_EXHAUSTED_MSG
from app.streaming import stream_reply

R = {}
cids = []
SYS = "You are Nervice, a helpful, friendly assistant. Keep replies to one short sentence."
_orig_create = llm._client.chat.completions.create

# A real 3-turn conversation. Turn 1 plants the fact; the question comes in turn 3 (the current turn).
WINDOW = [
    {"role": "user", "content": "Remember this about me: my dog is named Rex, a golden retriever."},
    {"role": "assistant", "content": "Got it — Rex the golden retriever. Noted."},
    {"role": "user", "content": "He's three years old, by the way."},
    {"role": "assistant", "content": "Three years old — good to know."},
]
Q = "Quick — what's my dog's name? Just the name."


def groq_429_on():
    async def boom(**kw):
        req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        resp = httpx.Response(429, request=req, text="Rate limit reached. Please try again in 30m0s.")
        raise RateLimitError("Rate limit reached.", response=resp, body=None)
    llm._client.chat.completions.create = boom


def groq_off():
    llm._client.chat.completions.create = _orig_create


def says_rex(reply):
    return bool(reply) and "rex" in reply.lower() and LADDER_EXHAUSTED_MSG not in reply


async def main():
    # ---- t1: REST chat_with_tools 429 fallback -> Claude must answer WITH the window ("Rex") ----
    groq_429_on()
    try:
        r1 = await chat_with_tools(SYS, WINDOW + [{"role": "user", "content": Q}],
                                   tools.TOOLS, tools.TOOL_FUNCS)
    finally:
        groq_off()
    R["t1 REST 429 fallback threads context"] = says_rex(r1)
    print(f"t1 (REST 429): reply={r1[:80]!r} -> {'PASS' if R['t1 REST 429 fallback threads context'] else 'FAIL'}")

    # ---- t2: consult / hard route (forced) -> Claude consult must see the window ("Rex") ----
    # Groq stays ON (the tool model fires consult_claude, then reformulates). The contextvar set in
    # chat_with_tools is what lets the generically-invoked consult tool thread the window.
    r2 = await chat_with_tools(SYS, WINDOW + [{"role": "user", "content": Q}],
                               tools.TOOLS, tools.TOOL_FUNCS, force_tool="consult_claude")
    R["t2 consult route threads context"] = says_rex(r2)
    print(f"t2 (consult): reply={r2[:80]!r} -> {'PASS' if R['t2 consult route threads context'] else 'FAIL'}")

    # ---- t3: WS streaming 429 fallback (the path that caused tonight's amnesia) -> "Rex" ----
    groq_429_on()
    frames = []
    async def send(f): frames.append(f)
    cid3 = "ctxtest-" + uuid.uuid4().hex[:8]; cids.append(cid3)
    try:
        r3 = await stream_reply("nate", Q, list(WINDOW), send, False, cid3)
    finally:
        groq_off()
    R["t3 WS streaming 429 fallback threads context"] = says_rex(r3)
    print(f"t3 (WS 429): reply={r3[:80]!r} -> {'PASS' if R['t3 WS streaming 429 fallback threads context'] else 'FAIL'}")

    # ---- t4: normal Groq turn (NOT capped) unchanged — still answers, still has context ----
    r4 = await chat_with_tools(SYS, WINDOW + [{"role": "user", "content": Q}],
                               tools.TOOLS, tools.TOOL_FUNCS)
    R["t4 normal Groq unchanged"] = says_rex(r4)
    print(f"t4 (normal Groq): reply={r4[:80]!r} -> {'PASS' if R['t4 normal Groq unchanged'] else 'FAIL'}")

    # ---- cleanup the one stored row (t3) ----
    import app.streaming as streaming
    for s in (getattr(streaming, "_pending", set()),):
        if s:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*list(s), return_exceptions=True), 20)
    from sqlalchemy import delete
    from app.db import AsyncSessionLocal
    from app.models import Message, Memory
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Message).where(Message.conversation_id.like("ctxtest-%")))
        await s.execute(delete(Memory).where(Memory.source_conv_id.like("ctxtest-%")))
        await s.commit()

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    ok = all(R.values())
    print("ALL PASS" if ok else "SOME FAILED")
    import os; os._exit(0 if ok else 1)


asyncio.run(main())
