"""Model tier-ladder tests: Groq->Claude fallback + Pro->Max account ladder + graceful exhaustion.
Mocks make t1/t3/t4 deterministic; t2/t5 use real Groq/Claude. Cleans up its DB rows. Throwaway."""
import sys, pathlib, asyncio, os, uuid, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import httpx
from groq import RateLimitError

import app.llm as llm
import app.agent as agent
import app.tools as tools
from app.chat import respond
from app.llm import chat_with_tools, LADDER_EXHAUSTED_MSG
from app.agent import AllClaudeExhausted

R = {}
cids = []
SYS = "You are Nervice, a helpful assistant. Keep replies short."
_orig_create = llm._client.chat.completions.create
_orig_ask = llm.ask_claude
_orig_ask_one = agent._ask_one


def make_429():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, request=req, text="Rate limit reached. Please try again in 30m0s.")
    return RateLimitError("Rate limit reached. Please try again in 30m0s.", response=resp, body=None)


def groq_429_on():
    async def boom(**kw):
        raise make_429()
    llm._client.chat.completions.create = boom


def groq_restore():
    llm._client.chat.completions.create = _orig_create
    llm.ask_claude = _orig_ask
    agent._ask_one = _orig_ask_one


async def main():
    # ---- t1: Groq 429 on a simple turn -> falls back to Claude, returns a REAL answer ----
    groq_429_on()
    async def fake_claude(prompt, system=None):
        return "Here's the answer from Claude (fallback worked)."
    llm.ask_claude = fake_claude
    try:
        r = await chat_with_tools(SYS, [{"role": "user", "content": "what's 2+2?"}],
                                  tools.TOOLS, tools.TOOL_FUNCS)
        R["t1 groq429->claude fallback"] = (r == "Here's the answer from Claude (fallback worked).")
        print(f"t1: reply={r!r}")
    finally:
        groq_restore()
    print("t1", "PASS" if R["t1 groq429->claude fallback"] else "FAIL")

    # ---- t2: hard question -> uses Claude via the Pro path (real) ----
    attempts = []
    async def spy_ask_one(task, system, name, config_dir):
        attempts.append(name)
        return await _orig_ask_one(task, system, name, config_dir)
    agent._ask_one = spy_ask_one
    try:
        ans = await agent.ask_claude(
            "Logic: all Bloops are Razzies; all Razzies are Lazzies. Are all Bloops necessarily "
            "Lazzies? Answer yes or no in one short sentence.")
        used_pro = attempts and attempts[0] == "pro"
        coherent = ans and "yes" in ans.lower() and LADDER_EXHAUSTED_MSG not in ans
        R["t2 hard->claude(pro)"] = bool(used_pro and coherent)
        print(f"t2: accounts tried={attempts}  answer={ans[:70]!r}")
    finally:
        groq_restore()
    print("t2", "PASS" if R["t2 hard->claude(pro)"] else "FAIL")

    # ---- t3: Pro fails -> ladder falls back to Max (mock the second account dir) ----
    async def ladder_mock(task, system, name, config_dir):
        if name == "pro":
            raise RuntimeError("simulated: Pro out of quota")          # Pro unavailable
        if name == "max":
            return f"[answered by MAX account, dir={config_dir.name}]"  # Max succeeds
        raise RuntimeError("unexpected account")
    agent._ask_one = ladder_mock
    try:
        ans = await agent.ask_claude("anything")
        R["t3 pro-fail->max"] = ("MAX account" in ans)
        print(f"t3: {ans!r}")
    finally:
        groq_restore()
    print("t3", "PASS" if R["t3 pro-fail->max"] else "FAIL")

    # ---- t4: EVERYTHING exhausted -> friendly message, no crash; speakable in voice mode ----
    # respond() RETURNING the friendly string (not raising) is exactly what makes /chat and /voice
    # return 200 instead of 500 (the endpoints do `reply = await respond(...)`). We stay on the main
    # loop (no TestClient second loop) and prove the reply is synthesizable so voice speaks it.
    groq_429_on()
    async def all_out(prompt, system=None):
        raise AllClaudeExhausted("simulated: all accounts out")
    llm.ask_claude = all_out
    try:
        core = await chat_with_tools(SYS, [{"role": "user", "content": "hello"}],
                                     tools.TOOLS, tools.TOOL_FUNCS)
        cid4 = "ladder-" + uuid.uuid4().hex[:8]; cids.append(cid4)
        voiced = await respond("nate", "hey, you there?", [], voice_mode=True)  # would-be /voice turn
    finally:
        groq_restore()
    import app.voice as v
    pcm, sr = v.synth_to_pcm(voiced)
    spoken = pcm.size > 0
    R["t4 exhausted->friendly,no500"] = (core == LADDER_EXHAUSTED_MSG
                                         and voiced == LADDER_EXHAUSTED_MSG and spoken)
    print(f"t4: core={core[:36]!r}  respond(voice)={voiced[:36]!r}  synthesizes={spoken}")
    print("t4", "PASS" if R["t4 exhausted->friendly,no500"] else "FAIL")

    # ---- t5: normal Groq turn still works when NOT capped (real) ----
    cid5 = "ladder-" + uuid.uuid4().hex[:8]; cids.append(cid5)
    reply = await respond("nate", "in one short sentence, what's a good reason to drink water?", [])
    R["t5 normal groq works"] = bool(reply) and LADDER_EXHAUSTED_MSG not in reply and len(reply.split()) > 3
    print(f"t5: reply={reply[:80]!r}")
    print("t5", "PASS" if R["t5 normal groq works"] else "FAIL")

    # ---- cleanup ----
    import app.api as api2, app.streaming as streaming
    for s in (getattr(api2, "_pending", set()), getattr(streaming, "_pending", set())):
        if s:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*list(s), return_exceptions=True), 20)
    from sqlalchemy import delete
    from app.db import AsyncSessionLocal
    from app.models import Message, Memory
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Message).where(Message.conversation_id.like("ladder-%")))
        await s.execute(delete(Memory).where(Memory.source_conv_id.like("ladder-%")))
        await s.commit()

    print("\nRESULT:")
    for k, v in R.items():
        print(("  PASS " if v else "  FAIL ") + k)
    print("ALL PASS" if all(R.values()) else "SOME FAILED")
    sys.exit(0 if all(R.values()) else 1)


asyncio.run(main())
